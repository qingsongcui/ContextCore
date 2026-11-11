import hashlib
import errno
import io
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.database import create_schema, db, encode, now_iso
from app.services import content_service, retrieval_service, semantic_service as semantic

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "_downloads", {})
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "semantic.db"))
    create_schema()
    with db() as conn:
        retrieval_service.ensure_schema(conn)
    return tmp_path


def fake_embedder(texts):
    result = []
    for text in texts:
        # Two distinguishable topics with fixed dimensions; no imported runtime.
        vector = [0.0] * 384
        vector[0 if any(word in text for word in ("数据库", "database")) else 1] = 1.0
        result.append(vector)
    return result


def test_default_search_neither_downloads_installs_nor_invokes_runtime(store, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Implicit external action")
    monkeypatch.setattr(semantic.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(semantic.subprocess, "run", forbidden)
    monkeypatch.setattr(semantic, "download_model", forbidden)
    monkeypatch.setattr(semantic, "embed_texts", forbidden)
    assert retrieval_service.get_settings()["semantic_enabled"] is False
    content_service.create_content("数据库", "数据库迁移")
    assert retrieval_service.search("数据库")["items"]
    result = retrieval_service.search("database", mode="hybrid")
    assert "semantic_disabled" in result["degraded"]
    semantic_mode = retrieval_service.search("database", mode="semantic")
    assert "semantic_disabled" in semantic_mode["degraded"]
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.routers.retrieval import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    http = client.post("/api/retrieval/search", json={"query": "数据库", "mode": "hybrid"})
    assert http.status_code == 200
    assert "semantic_disabled" in http.json()["degraded"]
    from app import mcp_server
    from app.security import authenticated_agent
    identity = authenticated_agent.set("codex")
    try:
        found = mcp_server.search_memory("数据库")
        assert found["items"]
    finally:
        authenticated_agent.reset(identity)
    assert semantic.get_status()["enabled"] is False
    assert semantic.get_status()["auto_download"] is False
    assert semantic.get_status()["network_on_search"] is False


def test_unconfirmed_model_download_never_contacts_network(store, monkeypatch):
    monkeypatch.setattr(semantic.urllib.request, "urlopen", lambda *a, **k: pytest.fail("No confirmed download"))
    with pytest.raises(HTTPException) as error:
        semantic.download_model()
    assert error.value.status_code == 422


def test_verified_local_pack_tamper_and_no_runtime_install(store, monkeypatch):
    content = b"synthetic-test-model"
    monkeypatch.setattr(semantic, "MANIFEST", {**semantic.MANIFEST, "files": [
        {"path": "onnx/model.onnx", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]})
    monkeypatch.setattr(semantic, "_runtime_available", lambda: False)
    root = store / "synthetic-pack"
    (root / "onnx").mkdir(parents=True)
    (root / "onnx/model.onnx").write_bytes(content)
    assert semantic.verify_local_pack(str(root))["pack_verified"]
    retrieval_service.update_settings({"semantic_enabled": True})
    assert semantic.get_status()["status"] == "runtime_missing"
    with pytest.raises(HTTPException) as error:
        semantic.embed_texts(["test"])
    assert error.value.status_code == 409
    (root / "onnx/model.onnx").write_bytes(b"tampered")
    assert not semantic.get_status()["pack_verified"]
    with pytest.raises(HTTPException):
        semantic.verify_local_pack(str(root))


def test_model_pack_symlink_rejected(store, monkeypatch):
    target = store / "target"
    target.mkdir()
    link = store / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(HTTPException) as error:
        semantic.verify_local_pack(str(link))
    assert error.value.status_code == 422


def test_fake_embeddings_are_versioned_and_source_checked(store):
    database = content_service.create_content("数据库知识", "迁移数据库")
    recipe = content_service.create_content("食谱", "烹饪")
    semantic.index_content(database["id"], embedder=fake_embedder)
    semantic.index_content(recipe["id"], embedder=fake_embedder)
    hits = semantic.search("database", embedder=fake_embedder)
    assert hits[0]["entity_id"] == database["id"]
    content_service.update_content(database["id"], content="更正后的资料", expected_version=1)
    assert database["id"] not in {hit["entity_id"] for hit in semantic.search("database", embedder=fake_embedder)}
    semantic.index_content(database["id"], embedder=fake_embedder)
    assert next(hit for hit in semantic.search("database", embedder=fake_embedder) if hit["entity_id"] == database["id"])["version"] == 2
    content_service.delete_content(recipe["id"])
    assert recipe["id"] not in {hit["entity_id"] for hit in semantic.search("食谱", embedder=fake_embedder)}


def test_embedding_race_rejects_results_after_source_changes(store):
    item = content_service.create_content("数据库", "原始内容")
    def mutating_embedder(texts):
        content_service.update_content(item["id"], content="新的原话", expected_version=1)
        return fake_embedder(texts)
    with pytest.raises(HTTPException) as error:
        semantic.index_content(item["id"], embedder=mutating_embedder)
    assert error.value.status_code == 409
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_vectors").fetchone()[0] == 0


def test_invalid_embeddings_do_not_enter_index(store):
    item = content_service.create_content("数据库", "原始内容")
    for embedder in (lambda _: [[1.0]], lambda _: [[float("nan")] * 384], lambda _: [[0] * 384]):
        with pytest.raises(HTTPException) as error:
            semantic.index_content(item["id"], embedder=embedder)
        assert error.value.status_code == 503


def test_enabled_without_pack_degrades_to_lexical_without_calls(store, monkeypatch):
    content_service.create_content("数据库", "数据库可回查证据")
    retrieval_service.update_settings({"semantic_enabled": True})
    monkeypatch.setattr(semantic.subprocess, "run", lambda *a, **k: pytest.fail("No verified pack"))
    result = retrieval_service.search("数据库", mode="hybrid")
    assert result["items"] and result["coverage"]["semantic"] == "unavailable"
    assert result["degraded"]


def test_inference_worker_is_isolated_and_does_not_inherit_credentials(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-forward")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-do-not-forward")
    monkeypatch.setattr(semantic, "get_status", lambda: {"status": "ready", "pack_path": str(store)})
    monkeypatch.setattr(semantic, "_verify_files", lambda root: [])
    monkeypatch.setattr(semantic, "_runtime_python", lambda: "/synthetic/data/runtime/bin/python")
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps([[1.0] + [0.0] * 383]))
    monkeypatch.setattr(semantic.subprocess, "run", fake_run)
    assert semantic.embed_texts(["数据库"], query=True)[0][0] == 1.0
    command, options = calls[0]
    assert command[0] == "/synthetic/data/runtime/bin/python"
    assert command[1:3] == ["-I", "-c"]
    assert "OPENAI_API_KEY" not in options["env"] and "ANTHROPIC_API_KEY" not in options["env"]
    assert options["env"]["HF_HUB_OFFLINE"] == "1"
    assert options["cwd"] == str(store)
    assert json.loads(options["input"])["texts"] == ["query: 数据库"]


def test_semantic_only_fake_fusion_has_explicit_channel_and_global_scope(store):
    item = content_service.create_content("数据库", "数据库向量")
    semantic.index_content(item["id"], embedder=fake_embedder)
    result = retrieval_service.search("database", mode="semantic", embedder=fake_embedder)
    assert result["items"][0]["channels"] == ["semantic"]
    assert result["items"][0]["id"] == item["id"]
    assert result["coverage"]["scope"] == "global"


def test_download_uses_only_fixed_manifest_and_verifies_synthetic_bytes(store, monkeypatch):
    payload = b"synthetic-test-download"
    monkeypatch.setattr(semantic, "MANIFEST", {**semantic.MANIFEST, "files": [
        {"path": "onnx/model.onnx", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}]})
    requested = []
    def response(url, **kwargs):
        requested.append(url)
        return io.BytesIO(payload)
    monkeypatch.setattr(semantic.urllib.request, "urlopen", response)
    result = semantic.download_model(confirmed=True)
    assert result["pack_verified"]
    assert requested == [f"https://huggingface.co/{semantic.MODEL_ID}/resolve/{semantic.MODEL_REVISION}/onnx/model.onnx"]


@pytest.mark.parametrize("failure", ["network", "disk_full", "interrupted"])
def test_interrupted_download_removes_partial_file_and_keeps_lexical_search(store, monkeypatch, failure):
    payload = b"synthetic-partial-download"
    monkeypatch.setattr(semantic, "MANIFEST", {**semantic.MANIFEST, "files": [
        {"path": "onnx/model.onnx", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}]})
    item = content_service.create_content("数据库", "数据库迁移仍可检索")

    class BrokenResponse(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                if failure == "network":
                    raise semantic.urllib.error.URLError("SYNTHETIC_PRIVATE_ENDPOINT")
                raise KeyboardInterrupt()
            return super().read(4)

    if failure == "disk_full":
        monkeypatch.setattr(semantic.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(payload))
        original = semantic.tempfile.NamedTemporaryFile

        class FullDisk:
            def __init__(self, **kwargs):
                self.file = original(**kwargs)
                self.name = self.file.name
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.file.close()
            def write(self, block):
                self.file.write(block[:4])
                raise OSError(errno.ENOSPC, "SYNTHETIC_PRIVATE_STORAGE")

        monkeypatch.setattr(semantic.tempfile, "NamedTemporaryFile", FullDisk)
    else:
        monkeypatch.setattr(semantic.urllib.request, "urlopen", lambda *a, **k: BrokenResponse(payload))
    expected = KeyboardInterrupt if failure == "interrupted" else HTTPException
    with pytest.raises(expected) as error:
        semantic.download_model(confirmed=True)
    if failure != "interrupted":
        assert error.value.status_code == (507 if failure == "disk_full" else 502)
        assert "SYNTHETIC_PRIVATE" not in error.value.detail
    models = store / "models"
    assert not list(models.rglob(".download-*"))
    assert not list(models.rglob("model.onnx"))
    assert not semantic.get_status()["pack_verified"]
    assert item["id"] in {row["id"] for row in retrieval_service.search("数据库")["items"]}


def test_runtime_plan_is_read_only_and_pins_complete_mac_python312_dependencies(store, monkeypatch):
    monkeypatch.setattr(semantic.subprocess, "Popen", lambda *a, **k: pytest.fail("Plan must not start a process"))
    monkeypatch.setattr(semantic.urllib.request, "build_opener", lambda *a, **k: pytest.fail("Plan must not access network"))
    monkeypatch.setattr(semantic.platform, "machine", lambda: "arm64")
    plan = semantic.runtime_plan()
    pins = {x["name"]: x["version"] for x in plan["dependencies"]}
    assert pins["onnxruntime"] == "1.23.2" and pins["tokenizers"] == "0.22.1" and pins["numpy"] == "2.2.6"
    assert len(pins) == 23
    assert plan["download_bytes"]["selected"] == 45789302
    assert plan["network"]["pip_network"] is False
    assert plan["network"]["allowed_hosts"] == ["files.pythonhosted.org"]
    assert not Path(plan["target_directory"]).exists()
    assert plan["changes_project_environment"] is False


def _mock_runtime_prepare(monkeypatch, *, fail_phase=None):
    plan = semantic.runtime_plan()
    # The fixed plan has macOS wheels; tests can exercise the state machine on any CI host.
    plan["supported"] = True
    monkeypatch.setattr(semantic, "runtime_plan", lambda: plan)
    commands, fetched = [], []
    def fake_command(command, cwd, timeout, lock_fd=None):
        commands.append(command)
        if "venv" in command:
            binary = Path(command[-1]) / "bin" / "python"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b"synthetic-python")
            return ""
        if "pip" in command:
            if fail_phase == "pip":
                raise RuntimeError("installer stderr contains synthetic-private-text")
            return ""
        expected = json.loads(command[-1])
        return json.dumps({"imports_verified": fail_phase != "probe", "versions": expected})
    monkeypatch.setattr(semantic, "_run_runtime_command", fake_command)
    monkeypatch.setattr(semantic, "_fetch_runtime_wheel", lambda item, directory: fetched.append(item))
    return plan, commands, fetched


def test_runtime_preparation_requires_current_explicit_confirmation(store, monkeypatch):
    plan, commands, _ = _mock_runtime_prepare(monkeypatch)
    with pytest.raises(HTTPException) as error:
        semantic.prepare_runtime(confirmed=False, plan_id=plan["plan_id"])
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        semantic.prepare_runtime(confirmed=True, plan_id="stale-plan")
    assert error.value.status_code == 409
    assert commands == [] and not Path(plan["target_directory"]).exists()


def test_runtime_preparation_uses_owned_venv_offline_hash_locked_pip_and_is_idempotent(store, monkeypatch):
    plan, commands, fetched = _mock_runtime_prepare(monkeypatch)
    result = semantic.prepare_runtime(confirmed=True, plan_id=plan["plan_id"])
    assert result["status"] == "ready" and result["verified"]
    assert len(fetched) == 23 and all(x["url"].startswith("https://files.pythonhosted.org/") for x in fetched)
    assert commands[0][1:5] == ["-I", "-m", "venv", "--copies"]
    pip = next(command for command in commands if "pip" in command)
    assert pip[0] == str(Path(plan["target_directory"]) / "bin/python")
    assert all(flag in pip for flag in ["--isolated", "--no-index", "--no-deps", "--require-hashes", "--only-binary=:all:"])
    assert "--system-site-packages" not in commands[0]
    second = semantic.prepare_runtime(confirmed=True, plan_id=plan["plan_id"])
    assert second["already_ready"] and len(commands) == 3
    assert semantic._runtime_python() == result["python_path"]


@pytest.mark.parametrize("phase", ["pip", "probe"])
def test_runtime_failure_is_readable_retryable_and_keeps_baseline_available(store, monkeypatch, phase):
    plan, commands, _ = _mock_runtime_prepare(monkeypatch, fail_phase=phase)
    content_service.create_content("数据库", "基础数据库检索")
    result = semantic.prepare_runtime(confirmed=True, plan_id=plan["plan_id"])
    assert result["status"] == "failed" and result["retryable"]
    assert result["error_code"].startswith("runtime_") and result["error_code"].endswith("_failed")
    assert "synthetic-private-text" not in json.dumps(result)
    assert retrieval_service.search("数据库")["items"]
    assert semantic._runtime_python() is None
    # A new explicit action can repair the same owned location, with no global pip.
    repaired_plan, repaired_commands, _ = _mock_runtime_prepare(monkeypatch)
    repaired = semantic.prepare_runtime(confirmed=True, plan_id=repaired_plan["plan_id"])
    assert repaired["status"] == "ready" and len(repaired_commands) == 3


def test_runtime_target_and_manifest_tampering_are_rejected(store, monkeypatch):
    plan = semantic.runtime_plan()
    plan["supported"] = True
    monkeypatch.setattr(semantic, "runtime_plan", lambda: plan)
    target = Path(plan["target_directory"])
    target.mkdir(parents=True)
    (target / "foreign.txt").write_text("must remain")
    with pytest.raises(HTTPException) as error:
        semantic.prepare_runtime(confirmed=True, plan_id=plan["plan_id"])
    assert error.value.status_code == 409 and (target / "foreign.txt").read_text() == "must remain"
    monkeypatch.setattr(semantic, "_RUNTIME_LOCK_SHA256", "bad")
    with pytest.raises(HTTPException):
        semantic._runtime_assets()


def test_fixed_runtime_wheel_downloader_checks_host_hash_size_without_real_network(store, monkeypatch):
    payload = b"synthetic-wheel"
    item = {"filename": "synthetic-1-py3-none-any.whl", "url": "https://files.pythonhosted.org/packages/synthetic.whl",
            "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    class Opener:
        def open(self, url, timeout):
            assert url == item["url"]
            return io.BytesIO(payload)
    monkeypatch.setattr(semantic.urllib.request, "build_opener", lambda *args: Opener())
    semantic._fetch_runtime_wheel(item, store)
    assert (store / item["filename"]).read_bytes() == payload
    with pytest.raises(RuntimeError):
        semantic._fetch_runtime_wheel({**item, "filename": "other.whl", "url": "https://attacker.invalid/pkg"}, store)
    with pytest.raises(RuntimeError):
        semantic._fetch_runtime_wheel({**item, "filename": "bad.whl", "sha256": "0" * 64}, store)


def test_runtime_api_has_preview_and_strict_prepare_body(store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.retrieval import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    plan = client.get("/api/retrieval/semantic/runtime/plan").json()
    assert plan["plan_id"] and plan["dependencies"]
    denied = client.post("/api/retrieval/semantic/runtime/prepare", json={"confirmed": False, "plan_id": plan["plan_id"]})
    assert denied.status_code == 422
    arbitrary = client.post("/api/retrieval/semantic/runtime/prepare", json={"confirmed": True, "plan_id": plan["plan_id"], "pip_command": "arbitrary"})
    assert arbitrary.status_code == 422


def test_runtime_command_timeout_kills_owned_process_group_and_filters_environment(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit")
    observed = {}
    class HangingProcess:
        pid = 4242
        returncode = None
        calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise semantic.subprocess.TimeoutExpired("synthetic", timeout)
            return b"", b""
        def poll(self):
            return self.returncode
    process = HangingProcess()
    def popen(command, **kwargs):
        observed.update(kwargs)
        return process
    def killpg(pid, sig):
        observed["killed_pid"] = pid
        process.returncode = -9
    monkeypatch.setattr(semantic.subprocess, "Popen", popen)
    monkeypatch.setattr(semantic.os, "killpg", killpg)
    with pytest.raises(RuntimeError, match="runtime_command_timeout"):
        semantic._run_runtime_command(["synthetic"], store, 1, 7)
    assert observed["start_new_session"] is True and observed["pass_fds"] == (7,)
    assert observed["killed_pid"] == process.pid and process.calls == 2
    assert "OPENAI_API_KEY" not in observed["env"]


def test_abandoned_runtime_preparation_can_be_retried_without_automatic_work(store):
    semantic._set_runtime_state("preparing", phase="install_offline")
    state = semantic.get_runtime_status()
    assert state["status"] == "interrupted" and state["retryable"]
    assert semantic._runtime_python() is None


def test_download_cancel_while_reading_waits_for_cleanup_before_reporting_cancelled(store, monkeypatch):
    payload = b"synthetic-cancellable-model"
    monkeypatch.setattr(semantic, "MANIFEST", {**semantic.MANIFEST, "files": [
        {"path": "onnx/model.onnx", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}]})
    reading, release = threading.Event(), threading.Event()
    class WaitingResponse(io.BytesIO):
        def read(self, size=-1):
            reading.set()
            assert release.wait(5), "Test failed to release synthetic network read"
            return super().read(size)
    monkeypatch.setattr(semantic.urllib.request, "urlopen", lambda *a, **k: WaitingResponse(payload))
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(semantic.download_model_job, confirmed=True, download_id=key)
        try:
            assert reading.wait(5)
            with pytest.raises(HTTPException) as error:
                semantic.download_model_job(confirmed=True, download_id=str(uuid4()))
            assert error.value.status_code == 409
            receipt = semantic.cancel_download(key)
            assert receipt["status"] == "running" and receipt["cancellation_requested"]
            assert not future.done()  # Request acknowledgement is not terminal cancellation.
        finally:
            release.set()
        assert future.result(timeout=5) == {"download_id": key, "status": "cancelled"}
    assert not list((store / "models").rglob(".download-*"))
    assert not list((store / "models").rglob("model.onnx"))
    assert not semantic.get_status()["pack_verified"]


def test_download_cancel_before_start_blocks_late_request_without_network(store, monkeypatch):
    monkeypatch.setattr(semantic.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Cancelled download used network"))
    key = str(uuid4())
    assert semantic.cancel_download(key)["status"] == "cancelled"
    assert semantic.download_model_job(confirmed=True, download_id=key)["status"] == "cancelled"
    with pytest.raises(HTTPException) as error:
        semantic.cancel_download("../../outside")
    assert error.value.status_code == 422
    assert not (store / "models").exists()


def test_download_api_validates_id_and_late_cancel_reports_actual_completion(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.retrieval import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    monkeypatch.setattr(semantic, "download_model", lambda **kwargs: {"status": "ready", "pack_verified": True})
    key = str(uuid4())
    assert client.post('/api/retrieval/semantic/download', json={"confirmed": False, "download_id": key}).status_code == 422
    assert semantic._downloads == {}
    result = client.post('/api/retrieval/semantic/download', json={"confirmed": True, "download_id": key})
    assert result.status_code == 200 and result.json()["pack_verified"]
    cancelled = client.post('/api/retrieval/semantic/download/cancel', json={"download_id": key})
    assert cancelled.json()["status"] == "ready" and not cancelled.json()["cancellation_requested"]
    assert client.post('/api/retrieval/semantic/download', json={"confirmed": True, "download_id": key}).status_code == 409
    assert client.post('/api/retrieval/semantic/download/cancel', json={"download_id": "invalid"}).status_code == 422


def test_real_source_deletion_removes_semantic_vectors_and_search_rejects_tombstone(store):
    item = content_service.create_content("权威技术文档", "详细的本地检索架构规范")
    semantic.index_content(item["id"], embedder=fake_embedder)
    assert len(semantic.search("架构", embedder=fake_embedder)) == 1

    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_vectors WHERE entity_id=?", (item["id"],)).fetchone()[0] == 1

    # Delete the content: trigger removes vector synchronously
    content_service.delete_content(item["id"])
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_vectors WHERE entity_id=?", (item["id"],)).fetchone()[0] == 0
    assert semantic.search("架构", embedder=fake_embedder) == []

    # Even if a stale vector row somehow existed, search checks canonical active & tombstones
    fake_vec = [1.0 / math.sqrt(384)] * 384
    with db() as conn:
        conn.execute(
            "INSERT INTO semantic_vectors VALUES (?,?,?,?,?,?)",
            (item["id"], 1, semantic.MODEL_REVISION, 384, encode(fake_vec), now_iso()),
        )
    # Must be filtered out because entity is tombstoned/deleted
    assert semantic.search("架构", embedder=fake_embedder) == []


def test_download_resume_after_cancellation_skips_verified_files(store, monkeypatch):
    file1_bytes = b"model-file-one-payload"
    file2_bytes = b"model-file-two-payload"
    f1_hash = hashlib.sha256(file1_bytes).hexdigest()
    f2_hash = hashlib.sha256(file2_bytes).hexdigest()
    custom_manifest = {
        **semantic.MANIFEST,
        "files": [
            {"path": "onnx/model.onnx", "size": len(file1_bytes), "sha256": f1_hash},
            {"path": "tokenizer.json", "size": len(file2_bytes), "sha256": f2_hash},
        ],
    }
    monkeypatch.setattr(semantic, "MANIFEST", custom_manifest)

    downloads_opened = []
    cancel_event = threading.Event()

    class SimulatedResponse(io.BytesIO):
        def __init__(self, data, name):
            super().__init__(data)
            self.name = name

        def read(self, size=-1):
            if self.name == "tokenizer.json":
                cancel_event.set()
            return super().read(size)

    def fake_urlopen(url, timeout=60):
        filename = "model.onnx" if "model.onnx" in url else "tokenizer.json"
        downloads_opened.append(filename)
        payload = file1_bytes if filename == "model.onnx" else file2_bytes
        return SimulatedResponse(payload, filename)

    monkeypatch.setattr(semantic.urllib.request, "urlopen", fake_urlopen)

    # First attempt: cancelled during file 2
    key1 = str(uuid4())
    with pytest.raises(semantic._DownloadCancelled):
        semantic.download_model(confirmed=True, _cancel_event=cancel_event)

    # File 1 was completed, File 2 was cancelled
    model_dir = store / "models" / ("multilingual-e5-small-" + semantic.MODEL_REVISION[:12])
    assert (model_dir / "onnx/model.onnx").is_file()
    assert not (model_dir / "tokenizer.json").exists()

    # Second attempt: without cancellation, file 1 is skipped, only file 2 is downloaded
    downloads_opened.clear()
    pack_status = semantic.download_model(confirmed=True)
    assert pack_status["pack_verified"] is True
    assert downloads_opened == ["tokenizer.json"]  # file 1 was NOT re-downloaded
    assert (model_dir / "tokenizer.json").is_file()


def test_offline_worker_validation_rejects_zero_or_nan_vectors(store, monkeypatch):
    retrieval_service.update_settings({"semantic_enabled": True})
    state = {
        "status": "ready",
        "pack_path": str(store),
        "enabled": True,
    }
    monkeypatch.setattr(semantic, "get_status", lambda: state)
    monkeypatch.setattr(semantic, "_verify_files", lambda root: [])
    monkeypatch.setattr(semantic, "_runtime_python", lambda: "/usr/bin/python3")

    # Case 1: worker returns zero vector
    zero_vec = [[0.0] * 384]
    class ZeroResult:
        returncode = 0
        stdout = json.dumps(zero_vec)
    monkeypatch.setattr(semantic.subprocess, "run", lambda *a, **k: ZeroResult())
    with pytest.raises(HTTPException) as err_zero:
        semantic.embed_texts(["test text"])
    assert err_zero.value.status_code == 503
    assert "must not be zero" in err_zero.value.detail

    # Case 2: worker returns non-finite / NaN
    nan_vec = [[float("nan")] * 384]
    class NanResult:
        returncode = 0
        stdout = json.dumps(nan_vec)
    monkeypatch.setattr(semantic.subprocess, "run", lambda *a, **k: NanResult())
    with pytest.raises(HTTPException) as err_nan:
        semantic.embed_texts(["test text"])
    assert err_nan.value.status_code == 503
    assert "invalid" in err_nan.value.detail

    # Case 3: worker returns nonzero exit code
    class FailResult:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(semantic.subprocess, "run", lambda *a, **k: FailResult())
    with pytest.raises(HTTPException) as err_fail:
        semantic.embed_texts(["test text"])
    assert err_fail.value.status_code == 503
    assert "worker failed" in err_fail.value.detail

    # Case 4: worker returns null or non-list JSON
    class NullResult:
        returncode = 0
        stdout = "null"
    monkeypatch.setattr(semantic.subprocess, "run", lambda *a, **k: NullResult())
    with pytest.raises(HTTPException) as err_null:
        semantic.embed_texts(["test text"])
    assert err_null.value.status_code == 503
    assert "count mismatch" in err_null.value.detail

    # Case 5: worker returns scalar vector instead of list of lists
    class ScalarResult:
        returncode = 0
        stdout = "[1.234]"
    monkeypatch.setattr(semantic.subprocess, "run", lambda *a, **k: ScalarResult())
    with pytest.raises(HTTPException) as err_scalar:
        semantic.embed_texts(["test text"])
    assert err_scalar.value.status_code == 503
    assert "dimensions or values are invalid" in err_scalar.value.detail

    # Case 6: worker returns non-numeric vector elements
    class NonNumericResult:
        returncode = 0
        stdout = json.dumps([["invalid_string"] * 384])
    monkeypatch.setattr(semantic.subprocess, "run", lambda *a, **k: NonNumericResult())
    with pytest.raises(HTTPException) as err_nonnum:
        semantic.embed_texts(["test text"])
    assert err_nonnum.value.status_code == 503
    assert "dimensions or values are invalid" in err_nonnum.value.detail

    # Case 7: worker returns huge numbers that overflow float or norm sum
    class OverflowResult:
        returncode = 0
        stdout = json.dumps([[1e300] * 384])  # 384 * (1e300)**2 overflows float (1e600 > 1.79e308)
    monkeypatch.setattr(semantic.subprocess, "run", lambda *a, **k: OverflowResult())
    with pytest.raises(HTTPException) as err_overflow:
        semantic.embed_texts(["test text"])
    assert err_overflow.value.status_code == 503
    assert "dimensions or values are invalid" in err_overflow.value.detail


def test_index_content_fences_commit_when_semantic_revoked_during_worker(store, monkeypatch):
    item = content_service.create_content("权威文档", "正在建立向量索引")
    retrieval_service.update_settings({"semantic_enabled": True})
    state = {
        "status": "ready",
        "pack_path": str(store),
        "enabled": True,
    }
    monkeypatch.setattr(semantic, "get_status", lambda: state)
    monkeypatch.setattr(semantic, "_verify_files", lambda root: [])
    monkeypatch.setattr(semantic, "_runtime_python", lambda: "/usr/bin/python3")

    valid_vector = [[0.5] * 384]
    class WorkerWithRevocation:
        returncode = 0
        stdout = json.dumps(valid_vector)

    def run_worker_and_revoke(*args, **kwargs):
        # While the embedding worker subprocess is running, user revokes semantic consent
        retrieval_service.update_settings({"semantic_enabled": False})
        return WorkerWithRevocation()

    monkeypatch.setattr(semantic.subprocess, "run", run_worker_and_revoke)

    # Production API route (embedder=None): commit must be fenced when consent is revoked
    with pytest.raises(HTTPException) as err_revoked:
        semantic.index_content(item["id"])
    assert err_revoked.value.status_code == 409
    assert "disabled during indexing" in err_revoked.value.detail

    # Ensure no vector was committed into the database
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_vectors").fetchone()[0] == 0


def test_semantic_search_fences_results_when_semantic_revoked_during_worker(store, monkeypatch):
    item = content_service.create_content("语义检索知识", "包含核心检索逻辑")
    semantic.index_content(item["id"], embedder=fake_embedder)
    retrieval_service.update_settings({"semantic_enabled": True})

    state = {
        "status": "ready",
        "pack_path": str(store),
        "enabled": True,
    }
    monkeypatch.setattr(semantic, "get_status", lambda: state)
    monkeypatch.setattr(semantic, "_verify_files", lambda root: [])
    monkeypatch.setattr(semantic, "_runtime_python", lambda: "/usr/bin/python3")

    valid_query_vec = [[1.0 / math.sqrt(384)] * 384]
    class WorkerWithRevocation:
        returncode = 0
        stdout = json.dumps(valid_query_vec)

    def run_worker_and_revoke(*args, **kwargs):
        # While the query embedding worker subprocess is running, user revokes consent
        retrieval_service.update_settings({"semantic_enabled": False})
        return WorkerWithRevocation()

    monkeypatch.setattr(semantic.subprocess, "run", run_worker_and_revoke)

    # 1. Direct semantic search API route (embedder=None): fences results with 409
    with pytest.raises(HTTPException) as err_search:
        semantic.search("核心", embedder=None)
    assert err_search.value.status_code == 409
    assert "disabled during search" in err_search.value.detail

    # 2. Hybrid search via retrieval_service: gracefully degrades without crashing or leaking semantic hits
    retrieval_service.update_settings({"semantic_enabled": True})
    result = retrieval_service.search("核心", mode="hybrid")
    assert result["coverage"]["semantic"] == "unavailable"
    assert any("disabled during search" in reason for reason in result["degraded"])
    # Items return only via lexical channel, never semantic
    for hit in result["items"]:
        assert "semantic" not in hit["channels"]
