import os
import subprocess
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]


def test_fifo_head_does_not_block_checkout_inspection(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    os.mkfifo(git_dir / "HEAD")
    result = subprocess.run(
        [sys.executable, "-c",
         "from app.services.project_service import read_git_checkout; "
         "import sys; assert read_git_checkout(sys.argv[1]) is None", str(tmp_path)],
        cwd=BACKEND,
        capture_output=True, text=True, timeout=3,
    )
    assert result.returncode == 0, result.stderr


def test_regular_head_still_reports_branch(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "README").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=test@example.com", "-c", "user.name=test",
         "commit", "-q", "-m", "init"],
        check=True,
    )
    sys.path.insert(0, str(BACKEND))
    from app.services.project_service import read_git_checkout
    checkout = read_git_checkout(str(tmp_path))
    assert checkout is not None
    assert checkout["branch"] == "main"
    assert checkout["detached"] is False
    assert checkout["head"]
