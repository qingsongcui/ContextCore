"""Optional, pinned local embeddings. Search never installs or downloads anything."""
from __future__ import annotations

import hashlib
import errno
import json
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.config import settings
from app.database import db, decode, encode, get_app_setting, log_audit, now_iso, set_app_setting

MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
_RUNTIME_LOCK_SHA256 = "ce0a64369f2d80d5511e9005c9e1c5161b414616ff5eee02e8e890732f8a9840"
_RUNTIME_WHEELS_SHA256 = "66b992c4c5664a122dbd5f3a3cef317f76f8e2f91923788933b008eb436e4380"
_RUNTIME_FILES = Path(__file__).parent
# Read from the official HF blobs API on 2026-10-24; model weights were not fetched.
MANIFEST = {
    "model_id": MODEL_ID, "revision": MODEL_REVISION, "dimensions": 384,
    "max_length": 512, "query_prefix": "query: ", "passage_prefix": "passage: ",
    "license": "MIT",
    "license_source": f"https://huggingface.co/api/models/{MODEL_ID}/revision/{MODEL_REVISION}",
    "files": [
        {"path": "onnx/model.onnx", "size": 470268510,
         "sha256": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"},
        {"path": "tokenizer.json", "size": 17082730,
         "sha256": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"},
    ],
}


def ensure_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS semantic_vectors (
        entity_id TEXT PRIMARY KEY, content_version INTEGER NOT NULL,
        model_revision TEXT NOT NULL, dimensions INTEGER NOT NULL,
        vector TEXT NOT NULL, updated_at TEXT NOT NULL)""")


def manifest() -> dict:
    return {**MANIFEST, "download_bytes": sum(f["size"] for f in MANIFEST["files"]),
            "source": f"https://huggingface.co/{MODEL_ID}/tree/{MODEL_REVISION}",
            "auto_download": False, "runtime_auto_install": False}


def _runtime_assets() -> tuple[dict, Path]:
    lock = _RUNTIME_FILES / "semantic-runtime-requirements.txt"
    wheels = _RUNTIME_FILES / "semantic-runtime-wheels.json"
    if (not lock.is_file() or not wheels.is_file() or _digest(lock) != _RUNTIME_LOCK_SHA256
            or _digest(wheels) != _RUNTIME_WHEELS_SHA256):
        raise HTTPException(503, "Fixed semantic runtime manifest is unavailable or changed")
    return json.loads(wheels.read_text()), lock


def _runtime_target() -> Path:
    return settings.db_path.parent / "runtimes" / ("semantic-py312-" + _RUNTIME_WHEELS_SHA256[:12])


def _minimal_environment() -> dict:
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
           if key in os.environ}
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               OMP_NUM_THREADS="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1")
    return env


def get_runtime_status() -> dict:
    with db() as conn:
        state = get_app_setting("semantic_runtime", {}, conn)
    target = _runtime_target()
    python = target / "bin" / "python"
    valid = bool(state.get("status") == "ready" and state.get("manifest_sha256") == _RUNTIME_WHEELS_SHA256
                 and state.get("lock_sha256") == _RUNTIME_LOCK_SHA256 and state.get("python_path") == str(python))
    if valid:
        try:
            stat = python.stat()
            valid = (not target.is_symlink() and not target.parent.is_symlink() and not python.is_symlink()
                     and state.get("python_fingerprint") == [stat.st_size, stat.st_mtime_ns, stat.st_ino])
        except OSError:
            valid = False
    status = "ready" if valid else state.get("status", "not_prepared")
    if status == "ready" and not valid:
        status = "verification_required"
    if status == "preparing":
        # A crashed app may leave a subprocess finishing an install. The lock FD
        # is inherited by that child; only an unlocked record is safe to retry.
        lock_path = target.parent / ("semantic-py312-" + _RUNTIME_WHEELS_SHA256[:12] + ".lock")
        busy = False
        if lock_path.is_file() and not lock_path.is_symlink():
            import fcntl
            with lock_path.open("r") as lockfile:
                try:
                    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lockfile, fcntl.LOCK_UN)
                except BlockingIOError:
                    busy = True
        if not busy:
            status = "interrupted"
    return {"runtime_id": "semantic-py312-" + _RUNTIME_WHEELS_SHA256[:12], "status": status,
            "target_directory": str(target), "python_path": str(python) if valid else None,
            "phase": state.get("phase"), "error_code": state.get("error_code"),
            "error": state.get("error"), "retryable": status not in {"ready", "preparing"},
            "verified_at": state.get("verified_at"), "auto_install": False,
            "changes_project_environment": False, "verified": valid}


def _runtime_available() -> bool:
    return get_runtime_status()["verified"]


def _runtime_python() -> str | None:
    return get_runtime_status()["python_path"]


def runtime_plan() -> dict:
    assets, _ = _runtime_assets()
    arch = platform.machine().lower()
    if arch == "aarch64":
        arch = "arm64"
    supported = sys.platform == "darwin" and sys.version_info[:2] == (3, 12) and arch in assets["architectures"]
    mac_version = platform.mac_ver()[0]
    if mac_version:
        supported = supported and tuple(int(x) for x in mac_version.split(".")[:2]) >= (13, 0)
    files = assets["architectures"].get(arch, [])
    byte_sizes = [sum(x["size"] for x in items) for items in assets["architectures"].values()]
    state = get_runtime_status()
    material = f"{_RUNTIME_WHEELS_SHA256}:{_RUNTIME_LOCK_SHA256}:{_runtime_target()}:{arch}:3.12"
    return {**state, "plan_id": hashlib.sha256(material.encode()).hexdigest(),
            "python_version": "3.12", "architecture": arch, "supported": bool(supported),
            "platform_requirement": "macOS 13+; CPython 3.12; arm64 or x86_64",
            "dependencies": [{"name": x["name"], "version": x["version"]} for x in files],
            "download_bytes": {"min": min(byte_sizes), "max": max(byte_sizes),
                               "selected": sum(x["size"] for x in files), "kind": "fixed_wheel_sizes"},
            "estimated_disk_bytes": 350 * 1024 * 1024, "disk_estimate_kind": "conservative_estimate",
            "network": {"allowed_hosts": ["files.pythonhosted.org"], "discovery_network": False,
                        "package_source": "https://pypi.org", "pip_network": False,
                        "only_binary": True, "require_hashes": True,
                        "note": "Fixed wheel URLs and SHA256; pip installs offline from verified wheels"},
            "source": "Official PyPI fixed release metadata; 2026-10-24",
            "bootstrap": "CPython bundled ensurepip; offline, no system-site-packages",
            "model_download_included": False}


def _set_runtime_state(status: str, **fields) -> None:
    with db() as conn:
        set_app_setting("semantic_runtime", {"status": status, "updated_at": now_iso(), **fields}, conn)


def _run_runtime_command(command: list[str], cwd: Path, timeout: int, lock_fd: int | None = None) -> str:
    process = subprocess.Popen(command, cwd=str(cwd), env=_minimal_environment(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                               pass_fds=(lock_fd,) if lock_fd is not None else ())
    try:
        stdout, _ = process.communicate(timeout=timeout)
        if process.returncode != 0:
            raise RuntimeError("runtime_command_failed")
        return stdout.decode("utf-8")
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise RuntimeError("runtime_command_timeout") from None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


class _RuntimeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https" or urlsplit(newurl).hostname != "files.pythonhosted.org":
            raise RuntimeError("runtime_download_host_rejected")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _fetch_runtime_wheel(item: dict, directory: Path) -> None:
    target = directory / item["filename"]
    if target.is_file() and not target.is_symlink() and target.stat().st_size == item["size"] and _digest(target) == item["sha256"]:
        return
    if target.is_symlink() or Path(item["filename"]).name != item["filename"]:
        raise RuntimeError("runtime_wheel_path_rejected")
    parsed = urlsplit(item["url"])
    if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org" or parsed.username or parsed.password:
        raise RuntimeError("runtime_download_host_rejected")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RuntimeRedirect())
    temporary = None
    try:
        with opener.open(item["url"], timeout=60) as response, tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
            temporary = Path(output.name)
            size = 0
            digest = hashlib.sha256()
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > item["size"]:
                    raise RuntimeError("runtime_wheel_size_mismatch")
                digest.update(block)
                output.write(block)
        if size != item["size"] or digest.hexdigest() != item["sha256"]:
            raise RuntimeError("runtime_wheel_hash_mismatch")
        os.replace(temporary, target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


_RUNTIME_PROBE = r'''
import importlib.metadata, json, socket, sys
def no_network(*args, **kwargs): raise RuntimeError("Runtime verification is offline")
class OfflineSocket(socket.socket):
    connect = no_network
    connect_ex = no_network
    sendto = no_network
socket.socket = OfflineSocket
socket.create_connection = no_network
socket.getaddrinfo = no_network
import numpy, onnxruntime, tokenizers
expected = json.loads(sys.argv[1])
versions = {name:importlib.metadata.version(name) for name in expected}
if versions != expected or sys.version_info[:2] != (3,12): raise RuntimeError("Runtime versions differ")
print(json.dumps({"versions": versions, "imports_verified": True}))
'''


def prepare_runtime(*, confirmed: bool = False, plan_id: str | None = None) -> dict:
    """Explicit, fixed installation in a dedicated data-owned venv; never system pip."""
    plan = runtime_plan()
    if not confirmed or plan_id != plan["plan_id"]:
        raise HTTPException(422 if not confirmed else 409, "Confirm the current runtime preparation plan")
    if not plan["supported"]:
        raise HTTPException(409, "This fixed runtime requires macOS 13+ and CPython 3.12")
    if plan["verified"]:
        return {**get_runtime_status(), "already_ready": True}
    assets, lock = _runtime_assets()
    target = _runtime_target()
    if target.is_symlink() or target.parent.is_symlink():
        raise HTTPException(422, "Managed runtime directory must not be a symlink")
    marker = target / ".senrelay-runtime-owner"
    owner = plan["runtime_id"]
    if target.exists() and (marker.is_symlink() or not marker.is_file() or marker.read_text() != owner):
        raise HTTPException(409, "Runtime target is not owned by this preparation plan")
    target.parent.mkdir(parents=True, exist_ok=True)
    import fcntl  # The fixed preparation plan explicitly supports macOS only.
    lock_path = target.parent / (owner + ".lock")
    if lock_path.is_symlink():
        raise HTTPException(422, "Runtime preparation lock must not be a symlink")
    with lock_path.open("a") as lockfile:
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**get_runtime_status(), "status": "preparing", "retryable": False}
        try:
            if get_runtime_status()["verified"]:
                return {**get_runtime_status(), "already_ready": True}
            target.mkdir(exist_ok=True)
            marker.write_text(owner)
            phase = "create_environment"
            _set_runtime_state("preparing", phase=phase)
            _run_runtime_command([sys.executable, "-I", "-m", "venv", "--copies", str(target)], target.parent, 120, lockfile.fileno())
            python = target / "bin" / "python"
            if not python.is_file() or python.is_symlink():
                raise RuntimeError("runtime_python_missing")
            wheels = target / ".verified-wheels"
            if wheels.is_symlink():
                raise RuntimeError("runtime_wheel_directory_rejected")
            wheels.mkdir(exist_ok=True)
            phase = "download_fixed_wheels"
            _set_runtime_state("preparing", phase=phase)
            files = assets["architectures"][plan["architecture"]]
            for item in files:
                _fetch_runtime_wheel(item, wheels)
            phase = "install_offline"
            _set_runtime_state("preparing", phase=phase)
            command = [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                       "--no-input", "install", "--no-index", "--find-links", str(wheels),
                       "--no-deps", "--only-binary=:all:", "--require-hashes", "--force-reinstall", "-r", str(lock)]
            _run_runtime_command(command, target, 300, lockfile.fileno())
            phase = "verify_imports_and_versions"
            _set_runtime_state("preparing", phase=phase)
            expected = {item["name"]: item["version"] for item in files}
            output = _run_runtime_command([str(python), "-I", "-c", _RUNTIME_PROBE, json.dumps(expected)], target, 60, lockfile.fileno())
            verified = json.loads(output)
            if verified.get("versions") != expected or not verified.get("imports_verified"):
                raise RuntimeError("runtime_verification_failed")
            stat = python.stat()
            _set_runtime_state("ready", phase="complete", manifest_sha256=_RUNTIME_WHEELS_SHA256,
                lock_sha256=_RUNTIME_LOCK_SHA256, python_path=str(python),
                python_fingerprint=[stat.st_size, stat.st_mtime_ns, stat.st_ino], verified_at=now_iso())
            with db() as conn:
                log_audit(actor="user", action="semantic.runtime.prepare", target=owner, conn=conn)
        except Exception:
            # Never persist stderr or arbitrary exception text from package installers.
            messages = {"create_environment": "无法创建独立 Python 环境，当前运行版本需支持 venv。",
                        "download_fixed_wheels": "依赖下载或 SHA256 校验失败，已校验的下载会保留。",
                        "install_offline": "固定依赖的离线安装失败，项目和系统环境未改动。",
                        "verify_imports_and_versions": "运行环境的导入或固定版本验证未通过，尚未启用。"}
            failed_phase = locals().get("phase", "create_environment")
            _set_runtime_state("failed", phase=failed_phase, error_code=f"runtime_{failed_phase}_failed",
                error=messages[failed_phase] + "基础检索仍可用，可在页面重新确认后重试。")
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)
    return get_runtime_status()


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _verify_files(root: Path) -> list[dict]:
    if root.is_symlink() or not root.is_dir():
        raise HTTPException(422, "A local model-pack directory is required")
    verified = []
    for item in MANIFEST["files"]:
        path = root / item["path"]
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
            raise HTTPException(422, "Model pack must not contain symlinks")
        if not path.is_file() or path.stat().st_size != item["size"] or _digest(path) != item["sha256"]:
            raise HTTPException(422, f"Model pack verification failed: {item['path']}")
        stat = path.stat()
        verified.append({"path": item["path"], "size": stat.st_size,
                         "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino})
    return verified


def verify_local_pack(pack_path: str) -> dict:
    raw = Path(pack_path).expanduser()
    # Check before resolve so a symlink supplied as the pack itself is rejected.
    if raw.is_symlink():
        raise HTTPException(422, "Model pack must not be a symlink")
    root = raw.resolve()
    files = _verify_files(root)
    with db() as conn:
        set_app_setting("semantic_pack", {"path": str(root), "revision": MODEL_REVISION,
                        "files": files, "verified_at": now_iso()}, conn)
        log_audit(actor="user", action="semantic.pack.verify", target=MODEL_REVISION, conn=conn)
    return get_status()


def get_status() -> dict:
    with db() as conn:
        configured = get_app_setting("retrieval_settings", {}, conn)
        pack = get_app_setting("semantic_pack", {}, conn)
    runtime = _runtime_available()
    runtime_details = get_runtime_status()
    enabled = bool(configured.get("semantic_enabled", False))
    status = "disabled"
    error = None
    pack_valid = bool(pack and pack.get("revision") == MODEL_REVISION
                      and {x.get("path") for x in pack.get("files", [])} == {x["path"] for x in MANIFEST["files"]})
    if pack_valid:
        root = Path(pack.get("path", ""))
        for item in pack.get("files", []):
            path = root / item["path"]
            try:
                stat = path.stat()
                if path.is_symlink() or any(stat_value != item[key] for stat_value, key in (
                    (stat.st_size, "size"), (stat.st_mtime_ns, "mtime_ns"), (stat.st_ino, "inode"))):
                    pack_valid = False
            except OSError:
                pack_valid = False
    if enabled:
        status = "ready" if pack_valid and runtime else ("runtime_missing" if pack_valid else "pack_missing")
        if status != "ready":
            error = "请在页面准备独立运行环境，并下载或验证固定本地模型包"
    return {"enabled": enabled, "status": status, "model_id": MODEL_ID,
            "revision": MODEL_REVISION, "dimensions": MANIFEST["dimensions"],
            "pack_verified": pack_valid, "pack_path": pack.get("path") if pack_valid else None,
            "runtime_installed": runtime, "auto_download": False, "runtime_auto_install": False,
            "network_on_search": False, "download_bytes": manifest()["download_bytes"], "error": error,
            "runtime": runtime_details}


_download_lock = threading.RLock()
_downloads: dict[str, dict] = {}


class _DownloadCancelled(Exception):
    pass


def _download_key(download_id: str) -> str:
    try:
        return str(UUID(download_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "A valid download ID is required") from None


def _expire_downloads() -> None:
    cutoff = time.monotonic() - 3600
    for key, job in list(_downloads.items()):
        if job["status"] != "running" and job["updated"] < cutoff:
            del _downloads[key]
    if len(_downloads) >= 128:
        raise HTTPException(429, "Too many download receipts; retry later")


def cancel_download(download_id: str) -> dict:
    key = _download_key(download_id)
    with _download_lock:
        job = _downloads.get(key)
        if job is None:
            # A cancel can arrive before its download request. Keep a bounded
            # tombstone so that the late request cannot start network activity.
            _expire_downloads()
            job = {"status": "cancelled", "event": threading.Event(), "updated": time.monotonic()}
            _downloads[key] = job
        if job["status"] in {"running", "cancelled"}:
            job["event"].set()
        return {"download_id": key, "status": job["status"],
                "cancellation_requested": job["event"].is_set()}


def download_model_job(*, confirmed: bool = False, download_id: str | None = None) -> dict:
    if not confirmed:
        raise HTTPException(422, "Explicit model download confirmation is required")
    key = _download_key(download_id or str(uuid4()))
    with _download_lock:
        if key in _downloads:
            if _downloads[key]["status"] == "cancelled":
                return {"download_id": key, "status": "cancelled"}
            raise HTTPException(409, "Download ID already used; do not repeat the request")
        _expire_downloads()
        if any(job["status"] == "running" for job in _downloads.values()):
            raise HTTPException(409, "A model download is already running")
        job = {"status": "running", "event": threading.Event(), "updated": time.monotonic()}
        _downloads[key] = job
    try:
        result = download_model(confirmed=True, _cancel_event=job["event"])
    except _DownloadCancelled:
        result = {"status": "cancelled"}
    except BaseException:
        with _download_lock:
            job.update(status="failed", updated=time.monotonic())
        raise
    with _download_lock:
        job.update(status=result.get("status", "succeeded"), updated=time.monotonic())
    return {**result, "download_id": key}


def download_model(*, confirmed: bool = False, _cancel_event=None) -> dict:
    """Explicit user action only. Fixed host/revision/files; no import-time effects."""
    if not confirmed:
        raise HTTPException(422, "Explicit model download confirmation is required")
    def check_cancelled():
        if _cancel_event is not None and _cancel_event.is_set():
            raise _DownloadCancelled()

    check_cancelled()
    root = settings.db_path.parent / "models" / ("multilingual-e5-small-" + MODEL_REVISION[:12])
    if root.is_symlink() or root.parent.is_symlink():
        raise HTTPException(422, "Managed model directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    for item in MANIFEST["files"]:
        check_cancelled()
        target = root / item["path"]
        if target.is_symlink() or target.parent.is_symlink():
            raise HTTPException(422, "Managed model files must not be symlinks")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and not target.is_symlink() and target.stat().st_size == item["size"] and _digest(target) == item["sha256"]:
            continue
        url = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{item['path']}"
        temporary = None
        try:
            with urllib.request.urlopen(url, timeout=60) as response, tempfile.NamedTemporaryFile(
                dir=target.parent, delete=False, prefix=".download-") as output:
                temporary = Path(output.name)
                size = 0
                value = hashlib.sha256()
                while True:
                    check_cancelled()
                    block = response.read(1024 * 1024)
                    check_cancelled()
                    if not block:
                        break
                    size += len(block)
                    if size > item["size"]:
                        raise HTTPException(502, "Model download exceeds fixed manifest size")
                    value.update(block)
                    output.write(block)
            if size != item["size"] or value.hexdigest() != item["sha256"]:
                raise HTTPException(502, "Downloaded model does not match fixed manifest")
            check_cancelled()
            os.replace(temporary, target)
        except urllib.error.URLError as error:
            raise HTTPException(502, "Model download interrupted; retry the explicit download when the connection is available") from error
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise HTTPException(507, "Insufficient disk space for the model download; free space before retrying") from error
            raise HTTPException(502, "Model download could not be saved; check storage and retry explicitly") from error
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    with _download_lock:
        check_cancelled()
        return verify_local_pack(str(root))


_WORKER = r'''
import json, os, socket, sys
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
def no_network(*args, **kwargs):
    raise RuntimeError("Network is disabled in embedding worker")
class OfflineSocket(socket.socket):
    connect = no_network
    connect_ex = no_network
    sendto = no_network
socket.socket = OfflineSocket
socket.create_connection = no_network
socket.getaddrinfo = no_network
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
data = json.load(sys.stdin)
tokenizer = Tokenizer.from_file(os.path.join(data["root"], "tokenizer.json"))
tokenizer.enable_truncation(max_length=512)
tokenizer.enable_padding()
tokens = tokenizer.encode_batch(data["texts"])
ids = np.array([x.ids for x in tokens], dtype=np.int64)
mask = np.array([x.attention_mask for x in tokens], dtype=np.int64)
options = ort.SessionOptions()
options.intra_op_num_threads = 1
options.inter_op_num_threads = 1
session = ort.InferenceSession(os.path.join(data["root"], "onnx/model.onnx"), sess_options=options,
                               providers=["CPUExecutionProvider"])
inputs = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
hidden = session.run(None, {x.name:inputs[x.name] for x in session.get_inputs()})[0]
pooled = (hidden * mask[...,None]).sum(axis=1) / mask.sum(axis=1)[:,None].clip(min=1)
pooled /= np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12)
print(json.dumps(pooled.tolist()))
'''


def embed_texts(texts: list[str], *, query: bool = False, embedder=None) -> list[list[float]]:
    if not texts:
        return []
    if len(texts) > 32 or any(len(text) > 32000 for text in texts):
        raise HTTPException(422, "Embedding batch is too large")
    prefixed = [("query: " if query else "passage: ") + text for text in texts]
    if embedder is not None:  # Local dependency injection for deterministic tests, never an API parameter.
        vectors = embedder(prefixed)
    else:
        state = get_status()
        if state["status"] != "ready":
            raise HTTPException(409, "Optional semantic runtime is not ready")
        root = Path(state["pack_path"])
        _verify_files(root)  # Strong check at inference boundary, not only a remembered path.
        runtime_python = _runtime_python()
        if runtime_python is None:
            raise HTTPException(409, "Prepare the verified isolated semantic runtime first")
        # The worker needs model files, not provider credentials or host config.
        env = _minimal_environment()
        try:
            result = subprocess.run([runtime_python, "-I", "-c", _WORKER],
                input=json.dumps({"root": str(root), "texts": prefixed}), text=True,
                capture_output=True, timeout=60, env=env, cwd=str(root), check=False)
            if result.returncode != 0:
                raise HTTPException(503, "Isolated embedding worker failed")
            vectors = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            raise HTTPException(503, "Isolated embedding worker unavailable") from exc
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise HTTPException(503, "Embedding result count mismatch")
    normalized = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != MANIFEST["dimensions"]:
            raise HTTPException(503, "Embedding dimensions or values are invalid")
        coords = []
        for x in vector:
            if type(x) not in (int, float):
                raise HTTPException(503, "Embedding dimensions or values are invalid")
            try:
                val = float(x)
            except (OverflowError, ValueError):
                raise HTTPException(503, "Embedding dimensions or values are invalid")
            if not math.isfinite(val):
                raise HTTPException(503, "Embedding dimensions or values are invalid")
            coords.append(val)
        try:
            norm = math.sqrt(sum(c ** 2 for c in coords))
        except OverflowError:
            raise HTTPException(503, "Embedding dimensions or values are invalid")
        if norm == 0 or not math.isfinite(norm):
            raise HTTPException(503, "Embedding vector must not be zero")
        normalized.append([c / norm for c in coords])
    return normalized


def index_content(entity_id: str, *, embedder=None) -> dict:
    from app.services.retrieval_service import _valid_content
    with db() as conn:
        ensure_schema(conn)
        item = _valid_content(conn, entity_id)
        if item is None:
            raise HTTPException(410, "Content is not active")
        version = item["version"]
        text = item["title"] + "\n" + item["content"]
    vector = embed_texts([text[:32000]], embedder=embedder)[0]
    with db() as conn:
        ensure_schema(conn)
        if embedder is None:
            configured = get_app_setting("retrieval_settings", {}, conn)
            if not configured.get("semantic_enabled", False):
                raise HTTPException(409, "Semantic retrieval was disabled during indexing")
        current = _valid_content(conn, entity_id, version)
        if current is None:
            raise HTTPException(409, "Content changed during embedding")
        conn.execute("INSERT OR REPLACE INTO semantic_vectors VALUES (?,?,?,?,?,?)",
            (entity_id, version, MODEL_REVISION, len(vector), encode(vector), now_iso()))
        log_audit(actor="user", action="semantic.index", target=entity_id, conn=conn)
    return {"entity_id": entity_id, "version": version, "model_revision": MODEL_REVISION}


def search(query: str, *, limit: int = 15, embedder=None) -> list[dict]:
    query_vector = embed_texts([query], query=True, embedder=embedder)[0]
    from app.services.retrieval_service import _valid_content
    hits = []
    with db() as conn:
        ensure_schema(conn)
        if embedder is None:
            configured = get_app_setting("retrieval_settings", {}, conn)
            if not configured.get("semantic_enabled", False):
                raise HTTPException(409, "Semantic retrieval was disabled during search")
        for row in conn.execute("SELECT * FROM semantic_vectors WHERE model_revision=?", (MODEL_REVISION,)):
            if _valid_content(conn, row["entity_id"], row["content_version"]) is None:
                continue
            vector = decode(row["vector"], [])
            if not isinstance(vector, list) or len(vector) != len(query_vector):
                continue
            try:
                score = sum(float(x) * float(y) for x, y in zip(query_vector, vector))
            except (ValueError, TypeError, OverflowError):
                continue
            if math.isfinite(score):
                hits.append({"entity_id": row["entity_id"], "version": row["content_version"], "score": score})
    return sorted(hits, key=lambda x: (-x["score"], x["entity_id"]))[:max(1, min(limit, 100))]
