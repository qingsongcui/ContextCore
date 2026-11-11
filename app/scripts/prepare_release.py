#!/usr/bin/env python3
"""Prepare a matched, immutable service/UI candidate. Never stops a service."""
import argparse
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def check_no_symlinks(path: Path) -> None:
    """Reject user-controlled links while accepting macOS's fixed temp aliases."""
    current = path.expanduser().absolute()
    while True:
        if current.is_symlink():
            system_alias = (
                sys.platform == "darwin"
                and current in (Path("/var"), Path("/tmp"))
                and current.resolve() == Path("/private") / current.name
            )
            if not system_alias:
                raise ValueError(f"Path must not contain symbolic links: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent


def prepare(backend: Path, frontend: Path, data: Path):
    check_no_symlinks(backend)
    check_no_symlinks(frontend)
    check_no_symlinks(data)
    backend, frontend, data = backend.resolve(), frontend.resolve(), data.resolve()
    sys.path.insert(0, str(backend))
    from app.release_resources import source_files
    files = source_files(backend)
    files += [backend / name for name in ("requirements.txt", "requirements-build.txt", "requirements-test.txt", "requirements-managed-formats.txt")
              if (backend / name).is_file()]
    files += [path for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml")
              if (path := backend / name).is_file()]
    manifest = {}
    sources = {}
    for path in sorted(files):
        check_no_symlinks(path)
        manifest[str(path.relative_to(backend))] = hashlib.sha256(path.read_bytes()).hexdigest()
        sources[str(path.relative_to(backend))] = path
    bootstrap = Path(__file__).with_name('launch_release.py')
    manifest['release_bootstrap.py'] = hashlib.sha256(bootstrap.read_bytes()).hexdigest()
    sources['release_bootstrap.py'] = bootstrap
    if "app/main.py" not in manifest:
        raise ValueError("Backend source is incomplete")
    from app.frontend_release import snapshot_frontend
    ui = snapshot_frontend(frontend, data)
    backend_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    release_id = f"{backend_hash[:16]}-{ui.name[:16]}"
    releases = data / "releases"
    if releases.is_symlink():
        raise ValueError("Release directory must not be a symbolic link")
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = releases / release_id
    launcher_hash = manifest['release_bootstrap.py']
    launcher = data/'launchers'/launcher_hash/'launch_release.py'
    for node in (data/'launchers', launcher.parent, launcher):
        if node.is_symlink():
            raise ValueError('Release launcher must not contain symbolic links')
    launcher.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not launcher.exists():
        with tempfile.NamedTemporaryFile(dir=launcher.parent, delete=False) as handle:
            temporary_launcher = Path(handle.name)
            handle.write(bootstrap.read_bytes())
        try:
            os.replace(temporary_launcher, launcher)
        finally:
            temporary_launcher.unlink(missing_ok=True)
    if hashlib.sha256(launcher.read_bytes()).hexdigest() != launcher_hash:
        raise ValueError('Release launcher verification failed')
    record = {"release_id": release_id, "backend_sha256": backend_hash,
              "frontend_sha256": ui.name, "frontend_dir": str(ui), "api_contract": 2,
              "backend_dir": str(target / "backend"), "files": manifest,
              'launcher_path': str(launcher), 'launcher_sha256': launcher_hash}
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix=".preparing-", dir=releases) as temporary:
            staged = Path(temporary) / "release"
            for relative, expected in manifest.items():
                destination = staged / "backend" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(sources[relative], destination)
                if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
                    raise ValueError("Backend changed while preparing release")
            (staged / "manifest.json").write_text(json.dumps(record, indent=2))
            os.rename(staged, target)
    if target.is_symlink():
        raise ValueError("Release target must not be a symbolic link")
    package = target / 'backend' / 'app'
    if package.is_symlink():
        raise ValueError("Prepared backend package must not be a symbolic link")
    actual = set()
    for path in (target / 'backend').rglob('*'):
        if path.is_symlink():
            raise ValueError("Prepared backend must not contain symbolic links")
        if '__pycache__' not in path.parts and path.is_file():
            actual.add(str(path.relative_to(target / 'backend')))
    if actual != set(manifest):
        raise ValueError("Prepared backend file set differs from manifest")
    manifest_path = target / 'manifest.json'
    if manifest_path.is_symlink() or json.loads(manifest_path.read_text()) != record:
        raise ValueError("Prepared release manifest verification failed")
    for relative, expected in manifest.items():
        path = target / "backend" / relative
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Prepared backend verification failed")
    return record
def activate_release(record: dict, data_dir: Path) -> Path:
    """Safely and atomically activate a prepared release candidate when the service is stopped."""
    check_no_symlinks(data_dir)
    data_dir = data_dir.resolve()

    # Validate all candidate bytes before changing the active pointer. Validation
    # must use the trusted control script, not imports from the proposed release.
    spec = importlib.util.spec_from_file_location(
        "contextcore_release_verifier", Path(__file__).with_name("launch_release.py"))
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    manifest = data_dir / "releases" / str(record.get("release_id")) / "manifest.json"
    verified = verifier.verified_record(
        manifest, require_active=False, require_launcher_identity=False)
    if verified != record:
        raise ValueError("Activation record differs from verified candidate")
    from app import runtime

    pid_file = data_dir / "service.pid"
    if pid_file.is_symlink():
        raise ValueError("Service PID file must not be a symbolic link")
    if pid_file.exists():
        running, active_pid = runtime.is_service_running(data_dir)
        if running:
            raise RuntimeError(
                f"ContextCore service is currently running (PID {active_pid}). "
                "Stop the service with 'contextcore stop' before activating a release."
            )
        info = runtime.read_pid_file(data_dir)
        if info is None:
            raise RuntimeError(
                "Service PID file exists but contains unreadable metadata; cannot verify service is stopped. "
                "Stop the service with 'contextcore stop' before activating a release."
            )
        pid = info.get("pid")
        if type(pid) is not int or pid <= 1:
            raise RuntimeError("Service PID metadata is invalid; cannot verify the service is stopped.")
        try:
            os.kill(pid, 0)
            raise RuntimeError(
                f"ContextCore process {pid} is still alive. "
                "Stop the service with 'contextcore stop' before activating a release."
            )
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise RuntimeError(
                    f"Cannot verify process {pid} is stopped ({exc}). "
                    "Stop the service with 'contextcore stop' before activating a release."
                ) from None

    active_path = data_dir / "current-release.json"
    if active_path.is_symlink():
        raise ValueError("current-release.json must not be a symbolic link")

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=data_dir, prefix=".activating-", delete=False) as tf:
            temp_name = Path(tf.name)
            os.fchmod(tf.fileno(), 0o600)
            json.dump(verified, tf, indent=2)
            tf.write("\n")
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(temp_name, active_path)
    finally:
        if temp_name is not None:
            temp_name.unlink(missing_ok=True)
    return active_path


if __name__ == "__main__":
    app_root = Path(__file__).resolve().parent.parent
    default_backend = app_root / "backend"
    default_frontend = app_root / "frontend" / "dist-staging"

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, default=default_backend if default_backend.exists() else None,
                        required=not default_backend.exists(), help="Path to backend directory")
    parser.add_argument("--frontend", type=Path, default=default_frontend if default_frontend.exists() else None,
                        required=not default_frontend.exists(), help="Path to built frontend directory")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Path to ContextCore data directory (default: platform canonical data dir)")
    parser.add_argument("--activate-release", action="store_true",
                        help="Safely activate prepared release into current-release.json if service is stopped")
    args = parser.parse_args()
    check_no_symlinks(args.backend)
    sys.path.insert(0, str(args.backend.resolve()))
    from app import runtime


    resolved_data = runtime.resolve_data_dir(args.data_dir)
    record = prepare(args.backend, args.frontend, resolved_data)
    if args.activate_release:
        active_path = activate_release(record, resolved_data)
        record["activated_path"] = str(active_path)

    print(json.dumps(record, indent=2))
