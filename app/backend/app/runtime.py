"""
ContextCore Runtime Service & Daemon Management.

Provides macOS launchd autostart generation, installation, uninstallation,
PID tracking with strict fail-closed identity verification, clean stop prevention
against launchd KeepAlive resurrection, and status inspection.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import plistlib
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LABEL = "com.contextcore.service"
DEFAULT_PORT = 8420
DEFAULT_HOST = "127.0.0.1"
DEFAULT_DATA_DIR_MACOS = Path.home() / "Library" / "Application Support" / "ContextCore"
DEFAULT_DATA_DIR_OTHER = Path.home() / ".contextcore"
PID_FILE_NAME = "service.pid"
SETTINGS_FILE_NAME = "service_settings.json"
SHUTDOWN_RECEIPT_FILE_NAME = "shutdown_receipt.json"


def get_default_data_dir() -> Path:
    """Return the platform-specific default data directory path."""
    env_dir = os.getenv("CONTEXTCORE_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    if sys.platform == "darwin":
        return DEFAULT_DATA_DIR_MACOS.expanduser().resolve()
    return DEFAULT_DATA_DIR_OTHER.expanduser().resolve()


def resolve_data_dir(data_dir: Path | str | None = None) -> Path:
    """Resolve data directory path, falling back to platform default."""
    if data_dir is not None:
        return Path(data_dir).expanduser().resolve()
    return get_default_data_dir()


def get_default_plist_path(label: str = DEFAULT_LABEL) -> Path:
    """Return the default macOS LaunchAgents plist path."""
    override_dir = os.getenv("CONTEXTCORE_LAUNCH_AGENTS_DIR")
    if override_dir:
        return Path(override_dir).expanduser().resolve() / f"{label}.plist"
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def get_autostart_desired(data_dir: Path | str | None = None) -> bool:
    """
    Get user preference for autostart.
    Defaults to True per PRD D047 ('默认开机自启，可关闭').
    """
    resolved_dir = resolve_data_dir(data_dir)
    settings_path = resolved_dir / SETTINGS_FILE_NAME
    if not settings_path.exists():
        return True
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        return bool(data.get("autostart_desired", True))
    except (OSError, json.JSONDecodeError):
        return True


def set_autostart_desired(
    desired: bool,
    data_dir: Path | str | None = None,
    label: str = DEFAULT_LABEL,
    plist_path: Path | str | None = None,
    stop_running_service: bool = False,
) -> dict[str, Any]:
    """
    Persist user preference for autostart to service_settings.json.
    When set to False:
    - Deactivates autostart by removing the plist from ~/Library/LaunchAgents (preventing launch on boot/login).
    - If stop_running_service=False (default for Web settings toggle), active service remains running
      until explicitly stopped via stop_service(), avoiding cutting off HTTP responses or violating
      the contract ('明确停止服务才停止工作').
    - If stop_running_service=True, unloads from launchctl to terminate the active service as well.
    """
    resolved_dir = resolve_data_dir(data_dir)
    target_plist = Path(plist_path).expanduser().resolve() if plist_path else get_default_plist_path(label)

    uninstalled_from_launchd = False
    service_stopped = False
    if not desired and sys.platform == "darwin":
        if target_plist.exists():
            is_owner, reason = verify_plist_ownership(target_plist, expected_label=label, expected_data_dir=resolved_dir)
            if not is_owner:
                return {
                    "ok": False,
                    "error": f"Refusing to deactivate autostart: plist ownership mismatch: {reason}",
                    "desired": True,
                    "installed": target_plist.exists(),
                    "registered": is_launchd_registered(label),
                }

            if stop_running_service:
                un_res = uninstall_autostart(data_dir=resolved_dir, label=label, plist_path=target_plist)
                if not un_res.get("ok"):
                    return {
                        "ok": False,
                        "error": f"Failed to deactivate autostart from launchd: {un_res.get('error')}",
                        "desired": True,
                        "installed": target_plist.exists(),
                        "registered": is_launchd_registered(label),
                    }
                uninstalled_from_launchd = True
                service_stopped = True
            else:
                try:
                    target_plist.unlink()
                    uninstalled_from_launchd = True
                except OSError as e:
                    return {
                        "ok": False,
                        "error": f"Failed to remove plist file: {e}",
                        "desired": True,
                        "installed": True,
                        "registered": is_launchd_registered(label),
                    }

    resolved_dir.mkdir(parents=True, exist_ok=True)
    settings_path = resolved_dir / SETTINGS_FILE_NAME

    current: dict[str, Any] = {}
    if settings_path.exists():
        try:
            current = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}

    current["autostart_desired"] = bool(desired)
    current["updated_at"] = datetime.now(timezone.utc).isoformat()

    temp_fd, temp_path = tempfile.mkstemp(dir=str(resolved_dir), prefix="settings_", suffix=".tmp")
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, settings_path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise

    return {
        "ok": True,
        "desired": bool(desired),
        "installed": target_plist.exists(),
        "registered": is_launchd_registered(label) if sys.platform == "darwin" else False,
        "uninstalled_from_launchd": uninstalled_from_launchd,
        "service_stopped": service_stopped,
        "settings_path": str(settings_path),
    }


def get_process_cmdline(pid: int) -> str | None:
    """Read command line for process ID using system ps tool. Fail-closed."""
    if pid <= 1:
        return None
    try:
        res = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            # macOS ps escapes non-ASCII argv in the C locale. Identity must
            # use the same UTF-8 rendering in both service and stopping CLI.
            env={**os.environ, "LC_ALL": "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"},
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def get_process_start_time(pid: int) -> str | None:
    """Read exact process start time (lstart) to detect recycled PIDs. Fail-closed."""
    if pid <= 1:
        return None
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "LC_ALL": "C"},
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def is_port_bound_by_pid(pid: int, port: int) -> bool:
    """
    Check if the given port is actively in LISTEN state by the specified PID.
    Uses lsof -a -p PID -iTCP:port -sTCP:LISTEN -Fp and matches exact line f"p{pid}".
    No loose substring matching; no socket connect fallback.
    """
    if pid <= 1 or port <= 0:
        return False
    try:
        lsof = '/usr/sbin/lsof' if sys.platform == 'darwin' and Path('/usr/sbin/lsof').is_file() else 'lsof'
        res = subprocess.run(
            [lsof, "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout:
            target_line = f"p{pid}"
            return any(line.strip() == target_line for line in res.stdout.splitlines())
    except Exception:
        pass
    return False


def get_process_uid(pid: int) -> int | None:
    """Read real user ID for process ID using system ps tool. Fail-closed."""
    if pid <= 1:
        return None
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "LC_ALL": "C"},
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout and res.stdout.strip():
            return int(res.stdout.strip())
    except Exception:
        pass
    return None


def _is_contextcore_service_cmdline(cmdline: str) -> bool:
    """Verify a command line belongs to a ContextCore / Senrelay service listener."""
    if not cmdline or not cmdline.strip():
        return False
    if check_cli_serve_tokens(cmdline):
        return True
    if re.search(r"(?:^|[/\s])senrelay-service(?:\s|$)", cmdline):
        return True
    if re.search(r"(?:^|[/\s])contextcore(?:\.exe)?\s+serve(?:\s|$)", cmdline):
        return True
    if re.search(r"(?:^|\s)uvicorn\s+app\.main:app(?:\s|$)", cmdline):
        return True
    return False


def find_current_user_port_owners(port: int = DEFAULT_PORT) -> list[int]:
    """
    Find current-UID ContextCore / senrelay-service PIDs actively listening on port.
    Never returns processes owned by other UIDs or unrelated applications.
    """
    if type(port) is not int or not 1 <= port <= 65535:
        return []
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    current_pid = os.getpid()
    owners: list[int] = []
    try:
        lsof = "/usr/sbin/lsof" if sys.platform == "darwin" and Path("/usr/sbin/lsof").is_file() else "lsof"
        res = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode != 0 or not res.stdout:
            return []
        candidate_pids: list[int] = []
        for raw_line in res.stdout.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("p") and stripped[1:].isdigit():
                cand = int(stripped[1:])
                if cand > 1 and cand != current_pid and cand not in candidate_pids:
                    candidate_pids.append(cand)
        for cand in candidate_pids:
            if current_uid is not None and get_process_uid(cand) != current_uid:
                continue
            cmdline = get_process_cmdline(cand)
            if not cmdline or not _is_contextcore_service_cmdline(cmdline):
                continue
            owners.append(cand)
    except Exception:
        return []
    return owners


def is_port_occupied(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> bool:
    """Return True if a TCP listener is accepting connections or bound in LISTEN state on port."""
    if type(port) is not int or not 1 <= port <= 65535:
        return False
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        pass
    try:
        lsof = "/usr/sbin/lsof" if sys.platform == "darwin" and Path("/usr/sbin/lsof").is_file() else "lsof"
        res = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout:
            return any(line.strip().startswith("p") and line.strip()[1:].isdigit() for line in res.stdout.splitlines())
    except Exception:
        pass
    return False


def stop_port_owner_processes(
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
    force: bool = False,
) -> dict[str, Any]:
    """
    Safely send SIGTERM to current-UID ContextCore / senrelay-service listeners on port
    and wait for both process termination and TCP port release.
    """
    owners = find_current_user_port_owners(port)
    if not owners:
        return {
            "ok": True,
            "message": "Service is not running",
            "stopped": False,
            "pid": None,
            "pids": [],
        }
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    signaled: list[int] = []
    for owner_pid in owners:
        if current_uid is not None and get_process_uid(owner_pid) != current_uid:
            continue
        cmdline = get_process_cmdline(owner_pid)
        if not cmdline or not _is_contextcore_service_cmdline(cmdline):
            continue
        if not is_port_bound_by_pid(owner_pid, port):
            continue
        try:
            os.kill(owner_pid, signal.SIGTERM)
            signaled.append(owner_pid)
        except OSError:
            pass

    if not signaled:
        return {
            "ok": True,
            "message": "Service is not running",
            "stopped": False,
            "pid": None,
            "pids": [],
        }

    deadline = time.monotonic() + max(0.2, float(timeout))
    while time.monotonic() < deadline:
        all_gone = all(_pid_definitively_absent(p) for p in signaled)
        remaining_owners = find_current_user_port_owners(port) if all_gone else signaled
        if all_gone and not remaining_owners:
            msg = f"Service listener on port {port} stopped gracefully (PID {', '.join(str(p) for p in signaled)})"
            return {
                "ok": True,
                "message": msg,
                "stopped": True,
                "pid": signaled[0],
                "pids": signaled,
            }
        time.sleep(0.1)

    if force:
        for owner_pid in signaled:
            if _pid_definitively_absent(owner_pid):
                continue
            if current_uid is not None and get_process_uid(owner_pid) != current_uid:
                continue
            cmdline = get_process_cmdline(owner_pid)
            if not cmdline or not _is_contextcore_service_cmdline(cmdline):
                continue
            try:
                os.kill(owner_pid, signal.SIGKILL)
            except OSError:
                pass
        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline:
            if all(_pid_definitively_absent(p) for p in signaled) and not find_current_user_port_owners(port):
                msg = f"Service listener on port {port} force-stopped (PID {', '.join(str(p) for p in signaled)})"
                return {
                    "ok": True,
                    "message": msg,
                    "stopped": True,
                    "pid": signaled[0],
                    "pids": signaled,
                }
            time.sleep(0.1)

    err = f"Service listener on port {port} (PID {signaled[0]}) did not stop within {timeout}s timeout."
    return {
        "ok": False,
        "error": err,
        "stopped": False,
        "pid": signaled[0],
        "pids": signaled,
    }


def _valid_interpreter_paths() -> set[Path]:
    """Return resolved interpreter binaries valid for the active Python environment."""
    candidates: set[Path] = set()
    for raw in (sys.executable, getattr(sys, "_base_executable", None)):
        if not raw:
            continue
        try:
            candidates.add(Path(raw).resolve())
        except (OSError, ValueError):
            continue
    for prefix_raw in (sys.prefix, getattr(sys, "base_prefix", None)):
        if not prefix_raw:
            continue
        try:
            root = Path(prefix_raw).resolve()
            app_py = root / "Resources/Python.app/Contents/MacOS/Python"
            if app_py.exists():
                candidates.add(app_py.resolve())
        except (OSError, ValueError):
            continue
    for resolved in list(candidates):
        try:
            app_py = resolved.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
            if app_py.exists():
                candidates.add(app_py.resolve())
        except (OSError, ValueError):
            continue
    return candidates


def check_cli_serve_tokens(cmdline: str) -> bool:
    """Recognize module serving or the console script installed for this interpreter."""
    if getattr(sys, 'frozen', False):
        prefix = str(Path(sys.executable).resolve()) + ' '
        if cmdline.startswith(prefix) and re.match(r'--(?:port|host|data-dir)(?:\s|=)', cmdline[len(prefix):]):
            return True
    if re.search(r"(?:^|\s)-m\s+app\.cli\s+serve(?:\s|$)", cmdline):
        return True
    valid_interpreters = _valid_interpreter_paths()
    script = Path(sys.prefix) / ("Scripts/contextcore.exe" if os.name == "nt" else "bin/contextcore")
    script_variants = [script]
    try:
        resolved_script = script.resolve()
        if resolved_script != script:
            script_variants.append(resolved_script)
    except (OSError, ValueError):
        resolved_script = script
    for candidate_script in script_variants:
        interpreter, separator, suffix = cmdline.partition(f" {candidate_script} serve")
        if separator and (not suffix or suffix[0].isspace()):
            try:
                if Path(interpreter).is_absolute() and Path(interpreter).resolve() in valid_interpreters:
                    return True
            except (OSError, ValueError):
                pass
    m = re.match(r"^(\S+)\s+(\S+)\s+serve(?:\s|$)", cmdline.strip())
    if m:
        interp_tok, script_tok = m.group(1), m.group(2)
        try:
            if (
                Path(interp_tok).is_absolute()
                and Path(interp_tok).resolve() in valid_interpreters
                and Path(script_tok).resolve() == resolved_script
            ):
                return True
        except (OSError, ValueError):
            return False
    return False



def check_data_dir_boundary(cmdline: str, expected_dir: Path | str) -> bool:
    """
    Verify that cmdline data-dir matches expected_dir:
    1. If '--data-dir' is present in cmdline:
       Extract argument and verify Path(extracted).resolve() == resolve_data_dir(expected_dir).resolve().
    2. If '--data-dir' is NOT in cmdline:
       The process ran with default invocation (canonical default data directory).
       Verify that resolve_data_dir(expected_dir).resolve() == get_default_data_dir().resolve().
    """
    target_resolved = resolve_data_dir(expected_dir).resolve()
    idx = cmdline.find("--data-dir")
    if idx != -1:
        rest = cmdline[idx + len("--data-dir"):].strip()
        if rest.startswith("="):
            rest = rest[1:].strip()
        m = re.split(r"\s+--(?:host|port|log-level|no-static|daemon|frontend-dir)(?:\s|=|$)", rest, maxsplit=1)
        extracted = m[0].strip()
        if not extracted:
            return False
        try:
            return Path(extracted).resolve() == target_resolved
        except Exception:
            return False
    # Default invocation without explicit --data-dir argument:
    # Must match the canonical default data directory
    return target_resolved == get_default_data_dir().resolve()

def verify_pid_identity(
    pid: int,
    expected_cmdline: str | None = None,
    expected_start_time: str | None = None,
    expected_port: int | None = None,
    data_dir: Path | str | None = None,
) -> bool:
    """
    Strict fail-closed verification that a running PID genuinely belongs to ContextCore.
    Prevents terminating unrelated processes if an OS recycled a dead PID.
    Checks:
    1. PID > 1 and process exists (os.kill 0).
    2. Read command line: fails closed if missing or empty.
    3. expected_cmdline, expected_start_time, and data_dir must ALL be provided (fails closed if missing).
    4. Command line must match expected_cmdline exactly.
    5. Command line must contain token sequence '-m app.cli serve'.
    6. Command line must contain --data-dir resolving to the exact data_dir.
    7. Read start_time: fails closed if query fails or returns empty.
    8. Start time must match expected_start_time exactly.
    9. If expected_port provided, verifies using lsof -a -p PID -iTCP:port -sTCP:LISTEN -Fp matching 'pPID'.
    """
    if type(pid) is not int or pid <= 1:
        return False

    if not isinstance(expected_cmdline, str) or not expected_cmdline.strip():
        return False
    if not isinstance(expected_start_time, str) or not expected_start_time.strip():
        return False
    if not isinstance(data_dir, (str, Path)):
        return False
    if expected_port is not None and (type(expected_port) is not int or not 1 <= expected_port <= 65535):
        return False

    # 1. Process existence check
    try:
        os.kill(pid, 0)
    except OSError:
        return False

    # 2. Command line query - FAIL CLOSED
    cmdline = get_process_cmdline(pid)
    if not cmdline or not cmdline.strip():
        return False

    # 3. Exact command line match against ground truth recorded on startup
    if cmdline.strip() != expected_cmdline.strip():
        return False

    # 4. Token sequence check: -m app.cli serve
    if not check_cli_serve_tokens(cmdline):
        return False

    # 5. Data directory boundary check
    if not check_data_dir_boundary(cmdline, data_dir):
        return False

    # 6. Process start time query - FAIL CLOSED
    current_start = get_process_start_time(pid)
    if not current_start or not current_start.strip():
        return False

    # 7. Start time match against ground truth recorded on startup
    if current_start.strip() != expected_start_time.strip():
        return False

    # 8. Expected port check via exact lsof -a -p PID -iTCP:port -sTCP:LISTEN -Fp
    if expected_port is not None:
        if not is_port_bound_by_pid(pid, expected_port):
            return False

    return True


def write_pid_file(
    pid: int,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    data_dir: Path | str | None = None,
) -> Path:
    """Write structured PID file with process identity metadata including data_dir."""
    resolved_dir = resolve_data_dir(data_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    pid_file = resolved_dir / PID_FILE_NAME

    cmdline = get_process_cmdline(pid) or ""
    start_time = get_process_start_time(pid) or ""

    payload = {
        "pid": pid,
        "port": port,
        "host": host,
        "data_dir": str(resolved_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cmdline": cmdline,
        "start_time": start_time,
        "app": "ContextCore",
    }

    temp_fd, temp_path = tempfile.mkstemp(dir=str(resolved_dir), prefix="pid_", suffix=".tmp")
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(temp_path, pid_file)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise

    return pid_file


def read_pid_file(data_dir: Path | str | None = None) -> dict[str, Any] | None:
    """Read and parse the PID file if it exists."""
    resolved_dir = resolve_data_dir(data_dir)
    pid_file = resolved_dir / PID_FILE_NAME
    if not pid_file.exists():
        return None
    try:
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "pid" in data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def remove_pid_file(data_dir: Path | str | None = None) -> bool:
    """Remove the PID file safely."""
    resolved_dir = resolve_data_dir(data_dir)
    pid_file = resolved_dir / PID_FILE_NAME
    try:
        if pid_file.exists():
            pid_file.unlink()
            return True
    except OSError:
        pass
    return False


def write_shutdown_receipt(
    data_dir: Path | str | None = None,
    status: str = "pending",
    pid: int | None = None,
    ok: bool | None = None,
    error: str | None = None,
    message: str | None = None,
    stopped: bool = False,
    requested_at: str | None = None,
) -> Path:
    """
    Write narrow metadata-only shutdown result receipt into data directory.
    Status must be one of: 'pending', 'stopped', 'failed'.
    Fails closed without leaking raw commands, tokens, or private content.
    Uses atomic file replacement.
    """
    if status not in ("pending", "stopped", "failed"):
        raise ValueError(f"Invalid shutdown status '{status}'; must be one of 'pending', 'stopped', 'failed'")

    resolved_dir = resolve_data_dir(data_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    receipt_file = resolved_dir / SHUTDOWN_RECEIPT_FILE_NAME

    existing = read_shutdown_receipt(resolved_dir)
    req_time = requested_at or (existing.get("requested_at") if existing else None)
    if not req_time:
        req_time = datetime.now(timezone.utc).isoformat()

    now_iso = datetime.now(timezone.utc).isoformat()
    completed_time = now_iso if status in ("stopped", "failed") else None

    sanitized_error = None
    if error:
        sanitized_error = str(error).strip()[:500]

    payload = {
        "status": status,
        "pid": pid if pid is not None else (existing.get("pid") if existing else None),
        "requested_at": req_time,
        "completed_at": completed_time,
        "ok": (status == "stopped") if ok is None else bool(ok),
        "error": sanitized_error,
        "message": str(message).strip()[:500] if message else None,
        "stopped": bool(stopped),
    }

    temp_fd, temp_path = tempfile.mkstemp(dir=str(resolved_dir), prefix="stop_receipt_", suffix=".tmp")
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(temp_path, receipt_file)
    except Exception:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise

    return receipt_file


def read_shutdown_receipt(data_dir: Path | str | None = None) -> dict[str, Any] | None:
    """Read and parse the shutdown receipt file if present and valid."""
    resolved_dir = resolve_data_dir(data_dir)
    receipt_file = resolved_dir / SHUTDOWN_RECEIPT_FILE_NAME
    if not receipt_file.exists():
        return None
    try:
        data = json.loads(receipt_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "status" in data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def clear_shutdown_receipt(data_dir: Path | str | None = None) -> bool:
    """Remove the shutdown receipt file safely."""
    resolved_dir = resolve_data_dir(data_dir)
    receipt_file = resolved_dir / SHUTDOWN_RECEIPT_FILE_NAME
    try:
        if receipt_file.exists():
            receipt_file.unlink()
            return True
    except OSError:
        pass
    return False

def is_service_running(data_dir: Path | str | None = None) -> tuple[bool, int | None]:
    """
    Check if ContextCore is actively running.
    Perform fail-closed identity verification without mutating PID metadata.
    A failed ps/lsof query is uncertainty, not proof that the process exited.
    Lifecycle operations alone may remove definitively stale records.
    """
    resolved_dir = resolve_data_dir(data_dir)
    info = read_pid_file(resolved_dir)
    if not info:
        return False, None

    pid = info.get("pid")
    if type(pid) is not int or pid <= 1:
        return False, None

    expected_port = info.get("port")
    expected_start = info.get("start_time")
    expected_cmd = info.get("cmdline")
    recorded_data_dir = info.get("data_dir")

    # All metadata fields required for fail-closed verification
    if not expected_cmd or not expected_start or not recorded_data_dir:
        return False, None

    if verify_pid_identity(
        pid,
        expected_cmdline=expected_cmd,
        expected_start_time=expected_start,
        expected_port=expected_port,
        data_dir=recorded_data_dir,
    ):
        return True, pid

    # Retain the record on identity mismatch or an unavailable probe so a later
    # status call can recover after transient ps/lsof failure. Never signal here.
    return False, None


def _pid_definitively_absent(pid: int | None) -> bool:
    """Only ESRCH or an exited zombie proves completion; query errors do not."""
    if type(pid) is not int or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.ESRCH
    try:
        state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                               capture_output=True, text=True, timeout=2.0, check=False)
        return state.returncode == 0 and isinstance(state.stdout, str) and state.stdout.strip().startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_python_path(explicit_python: Path | str | None = None) -> Path:
    """Select Python without dereferencing virtualenv interpreter symlinks."""
    if explicit_python:
        return Path(explicit_python).expanduser().absolute()

    # Active virtual environment
    env_venv = os.getenv("VIRTUAL_ENV")
    if env_venv:
        venv_py = Path(env_venv) / "bin" / "python"
        if venv_py.exists() and os.access(venv_py, os.X_OK):
            return venv_py.absolute()

    # Backend local .venv
    backend_dir = Path(__file__).resolve().parent.parent
    local_venv_py = backend_dir / ".venv" / "bin" / "python"
    if local_venv_py.exists() and os.access(local_venv_py, os.X_OK):
        return local_venv_py.absolute()

    # Workspace-level .venv
    root_venv_py = backend_dir.parent.parent / ".venv" / "bin" / "python"
    if root_venv_py.exists() and os.access(root_venv_py, os.X_OK):
        return root_venv_py.absolute()

    return Path(sys.executable).absolute()


def _verified_release_for_autostart(data_dir: Path, current_backend: Path) -> dict[str, Any] | None:
    """Validate a pinned release before changing any launchd configuration.

    The explicit active record wins even when reinstalling from a workspace CLI.
    An invalid record never silently falls back to the mutable development tree.
    """
    active = data_dir / "current-release.json"
    source = active if active.exists() or active.is_symlink() else current_backend.parent / "manifest.json"
    if not source.exists() and not source.is_symlink():
        # Missing metadata is development-only compatibility. A backend under
        # a release layout must never silently lose its matched browser assets,
        # including when invoked with a different selected data directory.
        if current_backend.is_relative_to(data_dir / "releases") or (current_backend.name == "backend" and current_backend.parent.parent.name == "releases"):
            raise ValueError("Published backend release manifest is unavailable")
        return None
    try:
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Invalid release manifest file")
        record = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("api_contract") != 2:
            raise ValueError("Invalid release manifest contract")
        backend_hash, frontend_hash = record.get("backend_sha256"), record.get("frontend_sha256")
        if not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in (backend_hash, frontend_hash)):
            raise ValueError("Invalid release content hashes")
        release_id = f"{backend_hash[:16]}-{frontend_hash[:16]}"
        if record.get("release_id") != release_id:
            raise ValueError("Release identity does not match content hashes")
        release_root = data_dir / "releases" / release_id
        backend = release_root / "backend"
        frontend = data_dir / "frontend-releases" / frontend_hash
        def verify_path(raw, expected):
            if not isinstance(raw, str) or not Path(raw).is_absolute() or Path(raw).resolve() != expected:
                raise ValueError("Release path does not belong to selected data directory")
            node = data_dir
            for component in expected.relative_to(data_dir).parts:
                node = node / component
                if node.is_symlink():
                    raise ValueError("Release paths must not contain symbolic links")
            if not expected.is_dir():
                raise ValueError("Release directory is unavailable")
        verify_path(record.get("backend_dir"), backend)
        verify_path(record.get("frontend_dir"), frontend)
        if source != active and current_backend != backend:
            raise ValueError("Release manifest does not describe the running backend")
        # The active pointer must agree with the immutable candidate manifest.
        manifest_path = release_root / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Pinned release manifest is unavailable")
        if json.loads(manifest_path.read_text(encoding="utf-8")) != record:
            raise ValueError("Active record differs from pinned release manifest")
        files = record.get("files")
        if not isinstance(files, dict) or "app/main.py" not in files or not files:
            raise ValueError("Backend release manifest is incomplete")
        if hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() != backend_hash:
            raise ValueError("Backend release manifest hash mismatch")
        if (backend / 'app').is_symlink():
            raise ValueError("Backend package must not be a symbolic link")
        actual_files = set()
        for path in backend.rglob('*'):
            # rglob deliberately does not descend directory symlinks. Reject
            # the node itself before is_file() can omit an unmanifested module.
            if path.is_symlink():
                raise ValueError("Backend release must not contain symbolic links")
            if '__pycache__' not in path.parts and path.is_file():
                actual_files.add(str(path.relative_to(backend)))
        if actual_files != set(files):
            raise ValueError("Backend release file set differs from manifest")
        for relative, expected_hash in files.items():
            if not isinstance(relative, str) or not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
                raise ValueError("Invalid backend file manifest")
            rel = Path(relative)
            if rel.is_absolute() or not rel.parts or any(part in (".", "..") for part in rel.parts):
                raise ValueError("Backend manifest path escapes release")
            path = backend
            for component in rel.parts:
                path = path / component
                if path.is_symlink():
                    raise ValueError("Backend file must not be a symbolic link")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise ValueError("Backend release content verification failed")
        from app.frontend_release import _digest_tree
        if not (frontend / "index.html").is_file() or _digest_tree(frontend) != frontend_hash:
            raise ValueError("Frontend release content verification failed")
        if record.get('launcher_path') or record.get('launcher_sha256'):
            launcher_hash = files.get('release_bootstrap.py')
            launcher = data_dir/'launchers'/str(launcher_hash)/'launch_release.py'
            if record.get('launcher_sha256') != launcher_hash or record.get('launcher_path') != str(launcher):
                raise ValueError('Release launcher identity mismatch')
            node = data_dir
            for component in launcher.relative_to(data_dir).parts:
                node /= component
                if node.is_symlink():
                    raise ValueError('Release launcher must not contain symbolic links')
            if not launcher.is_file() or hashlib.sha256(launcher.read_bytes()).hexdigest() != launcher_hash:
                raise ValueError('Release launcher content verification failed')
        return record
    except (OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Pinned release verification failed") from exc


def verified_serving_release(data_dir: Path, current_backend: Path, *,
                             frontend_dir: Path | None = None,
                             release_id: str | None = None) -> dict[str, Any] | None:
    """Validate a release claim before importing the app or opening its database."""
    backend = current_backend.resolve()
    pinned = backend.name == "backend" and backend.parent.parent.name == "releases"
    if not pinned and not release_id:
        return None
    record = _verified_release_for_autostart(data_dir, backend)
    if record is None or Path(record['backend_dir']).resolve() != backend:
        raise ValueError("Running backend does not match the published release")
    if release_id and release_id != record['release_id']:
        raise ValueError("Claimed release ID differs from verified content")
    if frontend_dir is not None and frontend_dir.resolve() != Path(record['frontend_dir']).resolve():
        raise ValueError("Requested frontend differs from matched release")
    return record


def generate_launchd_plist(
    data_dir: Path | str | None = None,
    python_path: Path | str | None = None,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    label: str = DEFAULT_LABEL,
) -> str:
    """
    Generate standard XML launchd plist for macOS background daemon.
    Configures KeepAlive with SuccessfulExit=False so that an explicit clean
    stop (exit 0) will NOT be resurrected by launchd.
    No desktop shell references; directly runs Python CLI serve.
    """
    resolved_dir = resolve_data_dir(data_dir)
    resolved_python = resolve_python_path(python_path)
    backend_dir = Path(__file__).resolve().parent.parent
    release = _verified_release_for_autostart(resolved_dir, backend_dir)
    if release:
        backend_dir = Path(release["backend_dir"]).resolve()

    log_dir = resolved_dir / "logs"

    program_arguments = [
        str(resolved_python),
        "-m",
        "app.cli",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
        "--data-dir",
        str(resolved_dir),
    ]

    if release:
        program_arguments.extend(["--frontend-dir", release["frontend_dir"]])
        if release.get('launcher_path'):
            program_arguments = [str(resolved_python), '-I', '-B', release['launcher_path'],
                                 '--manifest', str(Path(release['backend_dir']).parent/'manifest.json'),
                                 '--', *program_arguments[1:]]

    env_vars = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "PYTHONUNBUFFERED": "1",
        "CONTEXTCORE_DATA_DIR": str(resolved_dir),
        "CONTEXTCORE_DB": str(resolved_dir / "contextcore.db"),
        "PYTHONPATH": str(backend_dir),
    }

    if release:
        env_vars["CONTEXTCORE_RELEASE_ID"] = release["release_id"]

    plist_data: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(backend_dir),
        "EnvironmentVariables": env_vars,
        "RunAtLoad": True,
        # KeepAlive: crash recovery is enabled, but clean exit (status 0) on explicit stop
        # ensures launchd does NOT restart the process.
        "KeepAlive": {
            "SuccessfulExit": False,
            "Crashed": True,
        },
        "StandardOutPath": str(log_dir / "service.log"),
        "StandardErrorPath": str(log_dir / "service.err.log"),
    }

    xml_bytes = plistlib.dumps(plist_data, fmt=plistlib.FMT_XML)
    return xml_bytes.decode("utf-8")


def verify_plist_ownership(
    plist_path: Path,
    expected_label: str = DEFAULT_LABEL,
    expected_data_dir: Path | str | None = None,
) -> tuple[bool, str]:
    """
    Verify that an existing plist belongs to ContextCore and matches expected_data_dir.
    Returns (True, "OK") or (False, reason).
    """
    if not plist_path.exists():
        return True, "File does not exist"
    try:
        data = plistlib.loads(plist_path.read_bytes())
    except Exception as e:
        return False, f"Existing file is not a valid plist: {e}"

    if not isinstance(data, dict):
        return False, "Existing plist is not a dictionary"

    label = data.get("Label")
    if label != expected_label:
        return False, f"Plist label '{label}' does not match expected '{expected_label}'"

    args = data.get("ProgramArguments", [])
    if not any("app.cli" in str(arg) for arg in args) or not any("serve" in str(arg) for arg in args):
        return False, "Plist ProgramArguments do not contain ContextCore 'app.cli serve'"

    if expected_data_dir is not None:
        resolved_data_dir = str(resolve_data_dir(expected_data_dir))
        env_vars = data.get("EnvironmentVariables", {})
        plist_data_dir = env_vars.get("CONTEXTCORE_DATA_DIR")
        if not plist_data_dir:
            for i, arg in enumerate(args):
                if arg == "--data-dir" and i + 1 < len(args):
                    plist_data_dir = args[i + 1]
                    break
        if plist_data_dir and str(Path(plist_data_dir).resolve()) != str(Path(resolved_data_dir).resolve()):
            return False, f"Plist data directory mismatch: '{plist_data_dir}' != '{resolved_data_dir}'"

    return True, "OK"


def is_launchd_registered(label: str = DEFAULT_LABEL) -> bool:
    """Check if the given label is registered/loaded in launchctl."""
    if sys.platform != "darwin":
        return False
    try:
        uid = os.getuid()
        res = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0:
            return True
        # Legacy fallback
        res = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def _registered_launchd_pid(label: str) -> int | None:
    """Read the loaded job's process identity; registration alone is not ownership."""
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
        if result.returncode != 0 or not isinstance(result.stdout, str):
            return None
        matches = re.findall(r"(?m)^\s*pid = ([1-9]\d*)\s*$", result.stdout)
        if len(matches) == 1:
            return int(matches[0])
        if not matches and re.search(r"(?m)^\s*state = not running\s*$", result.stdout):
            return 0
        return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def is_launchd_installed(plist_path: Path | str | None = None, label: str = DEFAULT_LABEL) -> bool:
    """Check if the launchd plist file exists on disk."""
    path = Path(plist_path).expanduser().resolve() if plist_path else get_default_plist_path(label)
    return path.exists()


def install_autostart(
    data_dir: Path | str | None = None,
    python_path: Path | str | None = None,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    register: bool = True,
    label: str = DEFAULT_LABEL,
    plist_path: Path | str | None = None,
) -> dict[str, Any]:
    """
    Install macOS launchd plist and optionally load via launchctl.
    Verifies ownership before overwriting an existing plist; refuses foreign plist.
    Fails closed if registration fails.
    """
    if sys.platform != "darwin":
        return {
            "ok": False,
            "error": f"Launchd autostart is only supported on macOS (current: {sys.platform})",
            "platform": sys.platform,
            "supported": False,
        }

    resolved_dir = resolve_data_dir(data_dir)
    target_plist = Path(plist_path).expanduser().resolve() if plist_path else get_default_plist_path(label)

    # Ownership & data_dir verification: refuse foreign plist unconditionally
    if target_plist.exists():
        is_owner, reason = verify_plist_ownership(target_plist, expected_label=label, expected_data_dir=resolved_dir)
        if not is_owner:
            return {
                "ok": False,
                "error": f"Refusing to overwrite existing plist at {target_plist}: {reason}",
                "plist_path": str(target_plist),
                "installed": True,
                "registered": is_launchd_registered(label),
            }

    try:
        content = generate_launchd_plist(
            data_dir=resolved_dir, python_path=python_path, port=port, host=host, label=label,
        )
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": f"Autostart release verification failed: {exc}",
                "plist_path": str(target_plist), "installed": target_plist.exists(), "registered": is_launchd_registered(label)}

    resolved_dir.mkdir(parents=True, exist_ok=True)
    (resolved_dir / "logs").mkdir(parents=True, exist_ok=True)
    target_plist.parent.mkdir(parents=True, exist_ok=True)

    staged_plist: str | None = None
    try:
        fd, staged_plist = tempfile.mkstemp(dir=str(target_plist.parent), prefix=".contextcore-", suffix=".plist")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged_plist, target_plist)
        staged_plist = None
    except OSError:
        return {
            "ok": False,
            "error": "Autostart plist could not be written; the loaded job was not changed",
            "plist_path": str(target_plist),
            "installed": target_plist.exists(),
            "registered": is_launchd_registered(label),
        }
    finally:
        if staged_plist is not None:
            Path(staged_plist).unlink(missing_ok=True)

    # If already loaded and reloading, unregister first
    if register and is_launchd_registered(label):
        try:
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except Exception:
            pass


    registered = False
    if register:
        try:
            uid = os.getuid()
            res = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(target_plist)],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if res.returncode != 0:
                res = subprocess.run(
                    ["launchctl", "load", "-w", str(target_plist)],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )

            if res.returncode != 0:
                err_msg = res.stderr.strip() or res.stdout.strip()
                return {
                    "ok": False,
                    "error": f"launchctl load failed with exit code {res.returncode}: {err_msg}",
                    "plist_path": str(target_plist),
                    "installed": True,
                    "registered": False,
                    "label": label,
                }
            registered = True
        except Exception as e:
            return {
                "ok": False,
                "error": f"Failed to register with launchctl: {e}",
                "plist_path": str(target_plist),
                "installed": True,
                "registered": False,
                "label": label,
            }

    set_autostart_desired(True, resolved_dir, label=label, plist_path=target_plist)

    return {
        "ok": True,
        "plist_path": str(target_plist),
        "installed": True,
        "registered": registered,
        "label": label,
        "desired": True,
    }


def uninstall_autostart(
    data_dir: Path | str | None = None,
    label: str = DEFAULT_LABEL,
    plist_path: Path | str | None = None,
) -> dict[str, Any]:
    """
    Uninstall macOS launchd plist and unload from launchctl.
    Refuses foreign plist; fails closed if unload fails (retains plist).
    """
    if sys.platform != "darwin":
        return {
            "ok": False,
            "error": f"Launchd autostart is only supported on macOS (current: {sys.platform})",
            "platform": sys.platform,
            "supported": False,
        }

    resolved_dir = resolve_data_dir(data_dir)
    target_plist = Path(plist_path).expanduser().resolve() if plist_path else get_default_plist_path(label)

    if not target_plist.exists() and not is_launchd_registered(label):
        return {
            "ok": True,
            "uninstalled": True,
            "plist_path": str(target_plist),
            "unloaded": False,
            "message": "Autostart was not installed.",
            "label": label,
        }

    # Ownership verification: refuse foreign plist unconditionally
    if target_plist.exists():
        is_owner, reason = verify_plist_ownership(target_plist, expected_label=label, expected_data_dir=resolved_dir)
        if not is_owner:
            return {
                "ok": False,
                "error": f"Refusing to uninstall plist not owned by ContextCore at {target_plist}: {reason}",
                "plist_path": str(target_plist),
                "uninstalled": False,
            }

    unloaded = False
    if is_launchd_registered(label):
        try:
            uid = os.getuid()
            res = subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if res.returncode != 0 and target_plist.exists():
                res = subprocess.run(
                    ["launchctl", "unload", "-w", str(target_plist)],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )

            if res.returncode != 0:
                err_msg = res.stderr.strip() or res.stdout.strip()
                return {
                    "ok": False,
                    "error": f"Failed to unload launchd job (code {res.returncode}): {err_msg}. Plist file retained.",
                    "plist_path": str(target_plist),
                    "unloaded": False,
                    "uninstalled": False,
                }
            unloaded = True
        except Exception as e:
            return {
                "ok": False,
                "error": f"Exception unloading launchd job: {e}. Plist file retained.",
                "plist_path": str(target_plist),
                "unloaded": False,
                "uninstalled": False,
            }

    if target_plist.exists():
        try:
            target_plist.unlink()
        except OSError as e:
            return {
                "ok": False,
                "error": f"Failed to remove plist file: {e}",
                "plist_path": str(target_plist),
                "unloaded": unloaded,
                "uninstalled": False,
            }

    return {
        "ok": True,
        "uninstalled": True,
        "plist_path": str(target_plist),
        "unloaded": unloaded,
        "label": label,
    }


def stop_service(
    data_dir: Path | str | None = None,
    timeout: float = 5.0,
    label: str = DEFAULT_LABEL,
    force: bool = False,
    *,
    port: int = DEFAULT_PORT,
    stop_port_owner: bool = False,
) -> dict[str, Any]:
    """
    Explicitly stop the running ContextCore service.
    Avoids killing unrelated processes via strict fail-closed PID verification.
    If managed by launchd, verifies owned plist before touching launchd job, then
    uses bootout/unload to unregister active job so KeepAlive does not resurrect it,
    while preserving plist file on disk. Fails explicit on bootout/unload error.
    Does NOT escalate to SIGKILL unless force=True is explicitly passed.
    Re-verifies PID identity immediately prior to sending any signal.
    Records narrow metadata-only shutdown result receipt into data directory.
    """
    resolved_dir = resolve_data_dir(data_dir)
    pid_info = read_pid_file(resolved_dir)

    is_launchd = False
    if sys.platform == "darwin" and is_launchd_registered(label):
        is_launchd = True

    running, pid = is_service_running(resolved_dir)
    if not running:
        if pid_info and not _pid_definitively_absent(pid_info.get("pid")):
            error = "Service identity could not be verified; preserving PID metadata and refusing shutdown"
            write_shutdown_receipt(data_dir=resolved_dir, status="failed", pid=pid_info.get("pid"), ok=False, error=error, stopped=False)
            return {"ok": False, "error": error, "stopped": False, "pid": pid_info.get("pid")}
        # An explicit lifecycle operation may clean a definitively exited PID,
        # but must not unlink metadata replaced concurrently by a new process.
        if pid_info and read_pid_file(resolved_dir) == pid_info:
            remove_pid_file(resolved_dir)
        if stop_port_owner:
            port_res = stop_port_owner_processes(port=port, timeout=timeout, force=force)
            if port_res.get("stopped") or not port_res.get("ok", True):
                write_shutdown_receipt(
                    data_dir=resolved_dir,
                    status="stopped" if port_res.get("ok") else "failed",
                    pid=port_res.get("pid"),
                    ok=bool(port_res.get("ok")),
                    message=port_res.get("message"),
                    error=port_res.get("error"),
                    stopped=bool(port_res.get("stopped")),
                )
                return port_res
        write_shutdown_receipt(
            data_dir=resolved_dir,
            status="stopped",
            pid=pid_info.get("pid") if pid_info else None,
            ok=True,
            message="Service is not running",
            stopped=False,
        )
        return {
            "ok": True,
            "message": "Service is not running",
            "stopped": False,
            "pid": None,
        }

    assert pid is not None

    expected_cmd = pid_info.get("cmdline") if pid_info else None
    expected_start = pid_info.get("start_time") if pid_info else None
    expected_port = pid_info.get("port") if pid_info else None
    recorded_data_dir = pid_info.get("data_dir", str(resolved_dir)) if pid_info else str(resolved_dir)

    # 1. Pre-signal fail-closed identity verification
    if not verify_pid_identity(
        pid,
        expected_cmdline=expected_cmd,
        expected_start_time=expected_start,
        expected_port=expected_port,
        data_dir=recorded_data_dir,
    ):
        err = f"Refusing to send stop signal: PID {pid} failed fail-closed identity verification."
        write_shutdown_receipt(
            data_dir=resolved_dir,
            status="failed",
            pid=pid,
            ok=False,
            error=err,
            stopped=False,
        )
        return {
            "ok": False,
            "error": err,
            "stopped": False,
            "pid": pid,
        }

    if is_launchd:
        registered_pid = _registered_launchd_pid(label)
        if registered_pid is None:
            err = "Cannot verify whether the registered launchd job owns this service PID."
            write_shutdown_receipt(data_dir=resolved_dir, status="failed", pid=pid,
                                   ok=False, error=err, stopped=False)
            return {"ok": False, "error": err, "stopped": False, "pid": pid}
        # Another data directory may have the same global label registered.
        # Stop only our verified direct process; never boot out that other job.
        is_launchd = registered_pid == pid

    # 2. If managed by launchd, check owned plist before touching launchd job
    if is_launchd and sys.platform == "darwin":
        plist_path = get_default_plist_path(label)
        if not plist_path.exists():
            err = f"Cannot stop launchd-managed service: owned plist file not found at {plist_path}"
            write_shutdown_receipt(
                data_dir=resolved_dir,
                status="failed",
                pid=pid,
                ok=False,
                error=err,
                stopped=False,
            )
            return {
                "ok": False,
                "error": err,
                "stopped": False,
                "pid": pid,
            }

        is_owner, reason = verify_plist_ownership(plist_path, expected_label=label, expected_data_dir=resolved_dir)
        if not is_owner:
            err = f"Refusing to touch launchd job: plist ownership verification failed: {reason}"
            write_shutdown_receipt(
                data_dir=resolved_dir,
                status="failed",
                pid=pid,
                ok=False,
                error=err,
                stopped=False,
            )
            return {
                "ok": False,
                "error": err,
                "stopped": False,
                "pid": pid,
            }

        # Unload active job to prevent KeepAlive resurrection
        try:
            uid = os.getuid()
            bootout_res = subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if bootout_res.returncode != 0:
                unload_res = subprocess.run(
                    ["launchctl", "unload", str(plist_path)],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )
                if unload_res.returncode != 0:
                    err_detail = unload_res.stderr.strip() or unload_res.stdout.strip() or f"exit code {unload_res.returncode}"
                    err = f"Failed to unload launchd job ({label}): {err_detail}"
                    write_shutdown_receipt(
                        data_dir=resolved_dir,
                        status="failed",
                        pid=pid,
                        ok=False,
                        error=err,
                        stopped=False,
                    )
                    return {
                        "ok": False,
                        "error": err,
                        "stopped": False,
                        "pid": pid,
                    }
        except Exception as e:
            err = f"Exception unloading launchd job ({label}): {e}"
            write_shutdown_receipt(
                data_dir=resolved_dir,
                status="failed",
                pid=pid,
                ok=False,
                error=err,
                stopped=False,
            )
            return {
                "ok": False,
                "error": err,
                "stopped": False,
                "pid": pid,
            }

        if _pid_definitively_absent(pid):
            if read_pid_file(resolved_dir) == pid_info:
                remove_pid_file(resolved_dir)
            msg = f"Service stopped gracefully (PID {pid})"
            write_shutdown_receipt(
                data_dir=resolved_dir,
                status="stopped",
                pid=pid,
                ok=True,
                message=msg,
                stopped=True,
            )
            return {
                "ok": True,
                "message": msg,
                "stopped": True,
                "pid": pid,
            }

    # 3. Send SIGTERM to verified PID (re-verify identity right before kill)
    if not verify_pid_identity(
        pid,
        expected_cmdline=expected_cmd,
        expected_start_time=expected_start,
        expected_port=expected_port,
        data_dir=recorded_data_dir,
    ):
        err = f"PID {pid} identity changed before SIGTERM; aborting stop."
        write_shutdown_receipt(
            data_dir=resolved_dir,
            status="failed",
            pid=pid,
            ok=False,
            error=err,
            stopped=False,
        )
        return {
            "ok": False,
            "error": err,
            "stopped": False,
            "pid": pid,
        }

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    # 4. Poll for clean process termination
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pid_definitively_absent(pid):
            if read_pid_file(resolved_dir) == pid_info:
                remove_pid_file(resolved_dir)
            msg = f"Service stopped gracefully (PID {pid})"
            write_shutdown_receipt(
                data_dir=resolved_dir,
                status="stopped",
                pid=pid,
                ok=True,
                message=msg,
                stopped=True,
            )
            return {
                "ok": True,
                "message": msg,
                "stopped": True,
                "pid": pid,
            }
        time.sleep(0.1)

    # 5. Process did not exit within timeout
    if not force:
        # Fails closed without killing innocent process and does NOT remove PID file
        msg = f"Service (PID {pid}) did not stop within {timeout}s timeout. Use force=True to force terminate."
        write_shutdown_receipt(
            data_dir=resolved_dir,
            status="failed",
            pid=pid,
            ok=False,
            error=msg,
            message=msg,
            stopped=False,
        )
        return {
            "ok": False,
            "stopped": False,
            "message": msg,
            "pid": pid,
        }

    # 6. Force termination requested: re-verify identity right before SIGKILL
    if not verify_pid_identity(
        pid,
        expected_cmdline=expected_cmd,
        expected_start_time=expected_start,
        expected_port=expected_port,
        data_dir=recorded_data_dir,
    ):
        err = f"PID {pid} identity changed before SIGKILL; aborting force kill."
        write_shutdown_receipt(
            data_dir=resolved_dir,
            status="failed",
            pid=pid,
            ok=False,
            error=err,
            stopped=False,
        )
        return {
            "ok": False,
            "error": err,
            "stopped": False,
            "pid": pid,
        }

    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    except OSError:
        pass

    if not _pid_definitively_absent(pid):
        msg = f"Failed to force-stop service (PID {pid}); process still alive."
        write_shutdown_receipt(
            data_dir=resolved_dir,
            status="failed",
            pid=pid,
            ok=False,
            error=msg,
            message=msg,
            stopped=False,
        )
        return {
            "ok": False,
            "stopped": False,
            "message": msg,
            "pid": pid,
        }
    else:
        if read_pid_file(resolved_dir) == pid_info:
            remove_pid_file(resolved_dir)
        msg = f"Service force-stopped (PID {pid})"
        write_shutdown_receipt(
            data_dir=resolved_dir,
            status="stopped",
            pid=pid,
            ok=True,
            message=msg,
            stopped=True,
        )
        return {
            "ok": True,
            "message": msg,
            "stopped": True,
            "pid": pid,
        }

def autostart_status(
    data_dir: Path | str | None = None,
    label: str = DEFAULT_LABEL,
) -> dict[str, Any]:
    """
    Return comprehensive service and autostart status for one data directory.

    installed / registered describe only the LaunchAgent owned by this data_dir.
    A same-label plist for another directory is not installed for this directory.
    label_plist_present, plist_ownership and label_registered keep that other
    installation visible instead of hiding it or attributing it here.
    """
    resolved_dir = resolve_data_dir(data_dir)
    plist_path = get_default_plist_path(label)

    desired = get_autostart_desired(resolved_dir)
    label_plist_present = plist_path.exists()
    plist_owned_by_data_dir = False
    plist_ownership = "not_present"
    if label_plist_present:
        owned, reason = verify_plist_ownership(
            plist_path,
            expected_label=label,
            expected_data_dir=resolved_dir,
        )
        plist_owned_by_data_dir = owned
        plist_ownership = "owned" if owned else reason

    # File existence alone is the global label. Ownership binds it to this data_dir.
    installed = bool(label_plist_present and plist_owned_by_data_dir)
    label_registered = False
    if sys.platform == "darwin" and label_plist_present:
        label_registered = is_launchd_registered(label)
    registered = bool(installed and label_registered)

    running, pid = is_service_running(resolved_dir)
    pid_info = read_pid_file(resolved_dir) if running else None
    port = pid_info.get("port", DEFAULT_PORT) if pid_info else DEFAULT_PORT
    host = pid_info.get("host", DEFAULT_HOST) if pid_info else DEFAULT_HOST

    receipt = read_shutdown_receipt(resolved_dir)
    stop_status = receipt.get("status") if receipt else None
    stop_error = receipt.get("error") if receipt else None

    # Steady-state sanity check: if service is actively running and receipt says "stopped",
    # that receipt is from an earlier lifecycle and should not report as stopped.
    if running and stop_status == "stopped":
        stop_status = None
        stop_error = None

    return {
        "desired": desired,
        "autostart": desired,  # Contract compatibility alias
        "installed": installed,
        "autostart_installed": installed,  # Contract compatibility alias
        "registered": registered,
        "label_plist_present": label_plist_present,
        "plist_owned_by_data_dir": plist_owned_by_data_dir,
        "plist_ownership": plist_ownership,
        "label_registered": label_registered,
        "running": running,
        "platform": sys.platform,
        "supported": sys.platform == "darwin",
        "label": label,
        "plist_path": str(plist_path),
        "pid": pid,
        "port": port,
        "host": host,
        "data_dir": str(resolved_dir),
        "stop_status": stop_status,
        "stop_error": stop_error,
        "shutdown_receipt": receipt,
    }


def get_service_status_summary(data_dir: Path | str | None = None) -> dict[str, Any]:
    """
    Return compact service status matching the GET /api/status.service contract:
    {running: bool, autostart: bool, autostart_installed: bool, platform: str,
     stop_status?: str, stop_error?: str}

    autostart_installed follows ownership for this data_dir, not mere presence
    of the global label plist. Mismatch detail stays on autostart_status.
    """
    status = autostart_status(data_dir)
    res = {
        "running": status["running"],
        "autostart": status["desired"],
        "autostart_installed": status["installed"],
        "platform": status["platform"],
    }
    if status.get("stop_status") is not None:
        res["stop_status"] = status["stop_status"]
    if status.get("stop_error") is not None:
        res["stop_error"] = status["stop_error"]
    return res
