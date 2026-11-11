"""Pin browser assets at service startup instead of serving a mutable build tree."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from fastapi.staticfiles import StaticFiles


class PinnedStaticFiles(StaticFiles):
    """HTML always revalidates; a running process serves its pinned release."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Frontend releases must not contain symbolic links")
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def snapshot_frontend(source: Path, data_dir: Path) -> Path:
    if source.is_symlink() or not (source / "index.html").is_file():
        raise ValueError("A complete frontend distribution is required")
    release_root = data_dir / "frontend-releases"
    if release_root.is_symlink():
        raise ValueError("Frontend release directory must not be a symbolic link")
    release_root.mkdir(parents=True, exist_ok=True)
    digest = _digest_tree(source)
    target = release_root / digest
    if target.exists():
        if target.is_symlink() or _digest_tree(target) != digest:
            raise ValueError("Frontend release verification failed")
        return target
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=release_root) as temporary:
        staged = Path(temporary) / "release"
        shutil.copytree(source, staged, symlinks=True)
        if _digest_tree(staged) != digest:
            raise ValueError("Frontend changed during release preparation")
        os.rename(staged, target)
    return target
