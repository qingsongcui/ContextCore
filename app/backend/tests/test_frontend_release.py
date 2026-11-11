from pathlib import Path
import pytest
from app.frontend_release import snapshot_frontend


def test_running_release_does_not_change_when_build_directory_changes(tmp_path):
    source, data = tmp_path / "build", tmp_path / "data"
    source.mkdir()
    (source / "index.html").write_text("version-one")
    pinned = snapshot_frontend(source, data)
    assert snapshot_frontend(source, data) == pinned
    (source / "index.html").write_text("version-two")
    assert (pinned / "index.html").read_text() == "version-one"
    assert snapshot_frontend(source, data) != pinned


def test_release_rejects_symlinks_and_corrupt_cached_artifact(tmp_path):
    source = tmp_path / "build"
    source.mkdir()
    (source / "index.html").write_text("safe")
    pinned = snapshot_frontend(source, tmp_path / "data")
    (pinned / "index.html").write_text("modified")
    with pytest.raises(ValueError):
        snapshot_frontend(source, tmp_path / "data")
    (source / "external").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        snapshot_frontend(source, tmp_path / "data2")


def test_html_revalidates_and_release_is_pinned(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.frontend_release import PinnedStaticFiles
    source = tmp_path / "build"
    source.mkdir()
    (source / "index.html").write_text("first")
    pinned = snapshot_frontend(source, tmp_path / "data")
    app = FastAPI()
    app.mount("/", PinnedStaticFiles(directory=pinned, html=True))
    with TestClient(app) as client:
        (source / "index.html").write_text("next")
        response = client.get("/")
        assert response.text == "first"
        assert response.headers["cache-control"] == "no-cache, must-revalidate"


def test_pinned_frontend_flag_does_not_change_service_data_identity(tmp_path):
    from app.runtime import check_data_dir_boundary
    data = tmp_path / "Application Support" / "ContextCore"
    frontend = data / "frontend-releases" / "fixed"
    command = f"python -m app.cli serve --data-dir {data} --frontend-dir {frontend}"
    assert check_data_dir_boundary(command, data)
    assert not check_data_dir_boundary(command, tmp_path / "another")
    assert check_data_dir_boundary(command.replace("--frontend-dir ", "--frontend-dir="), data)
