"""Persistent, hash-checked configuration transactions.

Filesystem + SQLite are not atomic together. Each file has a durable intent and
private snapshot before replacement; interrupted operations enter recovery.
Only manifest-owned entries may be replaced/uninstalled. Never execute assets.
"""
from __future__ import annotations
from app.integrations.constants import MAX_CONNECTION_FILES


import copy
import difflib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import stat
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.integrations.secret_store import SecretStore

_LOCK = threading.RLock()
configuration_lock = _LOCK
SECRET_REF = re.compile(r"\{\{credential:(keychain:senrelay:[a-f0-9]{32})\}\}")
REFERENCE_VALUE = re.compile(r"(?:Bearer\s+)?\{\{credential:keychain:senrelay:[a-f0-9]{32}\}\}", re.I)
SENSITIVE = re.compile(r"(key|secret|password|auth|token|credential|cookie)", re.I)


class ConfigurationError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if SENSITIVE.search(key) else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = SECRET_REF.sub("[CREDENTIAL REFERENCE]", value)
        return re.sub(r"(?i)(bearer\s+)[^\s\"']+|sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    return value


def reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            safe_metadata = (key == "oauth" and isinstance(item, dict)) or key == "credential_ref" or (key == "token_endpoint_auth_method" and item == "none") or (key == "token_type" and item == "Bearer")
            if key == "credential_reattach_required" and isinstance(item, bool):
                safe_metadata = True
            if key == 'credential_source' and item in ('provider', 'account', None):
                safe_metadata = True
            if key in ("authorization_url", "token_url") and isinstance(item, str):
                from urllib.parse import urlsplit
                endpoint = urlsplit(item)
                safe_metadata = endpoint.scheme == "https" and bool(endpoint.hostname) and not (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment)
            if not safe_metadata and SENSITIVE.search(str(key)) and item not in (None, "", "[REDACTED]"):
                if not (isinstance(item, str) and REFERENCE_VALUE.fullmatch(item)):
                    raise ConfigurationError("Use a credential reference; plaintext secrets are not accepted", 422)
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str) and not REFERENCE_VALUE.fullmatch(value) and re.search(r"(?i)sk-[A-Za-z0-9_-]{8,}|bearer\s+[^\s\"']+|(?:authorization|x-api-key)\s*[:=]|--(?:api[-_]key|token|secret|password|auth)(?:[=\s]|$)", REFERENCE_VALUE.sub("[CREDENTIAL REFERENCE]", value)):
        raise ConfigurationError("Plaintext credentials cannot be stored in assets", 422)


def toml_inline_comment(text: str) -> str:
    quote = None
    escaped = False
    for index, character in enumerate(text):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = None
        elif character in ("'", '"'):
            quote = character
        elif character == "#":
            return text[index:]
    return ""


class ConfigurationTransactions:
    def __init__(self, db_path: Path, state_dir: Path, allowed_roots: list[Path], vault: SecretStore):
        self.db_path, self.state_dir, self.vault = Path(db_path), Path(state_dir), vault
        self.roots = [Path(root).absolute() for root in allowed_roots]
        self.ensure_schema()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def ensure_schema(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS management_plans (
              id TEXT PRIMARY KEY, version INTEGER NOT NULL, status TEXT NOT NULL,
              payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS management_owned (
              path TEXT NOT NULL, item_key TEXT NOT NULL, value_hash TEXT NOT NULL,
              owner TEXT NOT NULL, PRIMARY KEY(path,item_key))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS management_skill_files (
              path TEXT PRIMARY KEY, state TEXT NOT NULL)""")

    def safe_path(self, raw: str, *, directory: bool = False) -> Path:
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            raise ConfigurationError("Target must be an absolute path without traversal", 422)
        path = path.absolute()
        if not any(path.is_relative_to(root) and (directory or path != root) for root in self.roots):
            raise ConfigurationError("Target is outside the authorized configuration roots", 403)
        for part in [path, *path.parents]:
            if part.is_symlink():
                raise ConfigurationError("Symlink configuration targets are not supported", 409)
        if path.exists() and (not path.is_dir() if directory else not path.is_file()):
            raise ConfigurationError(
                "Configuration parent must be a directory" if directory else "Configuration target must be a regular file", 422
            )
        return path

    def _read(self, path: Path) -> bytes | None:
        self.safe_path(str(path))
        if not path.exists():
            return None
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ConfigurationError("Configuration exceeds the 4 MiB limit", 422)
        return path.read_bytes()

    def _skill_registry(self, path: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT state FROM management_skill_files WHERE path=?", (path,)).fetchone()
        return json.loads(row[0]) if row else None

    def _skill_path(self, raw: str) -> Path:
        # Only the final file may be a managed link. Validate the actual parent,
        # not a synthetic child whose name can collide with a legitimate file.
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            raise ConfigurationError("Unsafe managed Skill path", 422)
        self.safe_path(str(path.parent), directory=True)
        return path

    def _skill_target(self, state: dict) -> Path:
        owner, version, checksum = state.get("owner"), state.get("source_version"), state.get("bytes_hash")
        if not isinstance(owner, str) or not re.fullmatch(r"asset_[a-f0-9]{16}", owner) or type(version) is not int or version < 1 or not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ConfigurationError("Invalid canonical Skill identity")
        root = self.state_dir.parent / "skill-versions"
        target = root / owner / str(version) / checksum
        if state.get("target") != str(target) or state.get("target_hash") != digest(str(target).encode()):
            raise ConfigurationError("Invalid canonical Skill target")
        for part in [target, *target.parents]:
            if part.is_symlink():
                raise ConfigurationError("Canonical Skill store cannot contain symlinks")
            if part == root or part.is_relative_to(root):
                if part.exists() and (part.stat().st_uid != os.getuid() or part.stat().st_mode & 0o022):
                    raise ConfigurationError("Canonical Skill store is not privately owned")
        return target

    def _skill_bytes(self, state: dict) -> bytes:
        target = self._skill_target(state)
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 500000 or info.st_nlink != 1:
                    raise ConfigurationError("Canonical Skill target must be a bounded private regular file")
                data = stream.read(500001)
        except OSError:
            raise ConfigurationError("Canonical Skill version is unavailable") from None
        if digest(data) != state["bytes_hash"]:
            raise ConfigurationError("Canonical Skill version was externally edited")
        return data

    def _skill_read(self, raw: str, allowed: list[dict | None]) -> tuple[bytes | None, dict | None]:
        path = self._skill_path(raw)
        if path.is_symlink():
            # Compare the raw link first. Never open an attacker-chosen target.
            target = os.readlink(path)
            state = next((entry for entry in allowed if entry and entry["kind"] == "symlink" and entry["target"] == target), None)
            if state is None:
                raise ConfigurationError("Managed Skill link was externally repointed or is unowned")
            return self._skill_bytes(state), state
        data = self._read(path)
        state = {"kind": "copy", "bytes_hash": digest(data)} if data is not None else None
        if state:
            state = next((entry for entry in allowed if entry and entry["kind"] == "copy" and entry["bytes_hash"] == state["bytes_hash"]), state)
        return data, state

    def _skill_state(self, spec: dict, after: bytes | None) -> dict | None:
        if after is None:
            return None
        if "restore_skill_state" in spec:
            return spec["restore_skill_state"]
        descriptor = spec["managed_skill"]
        if descriptor["mode"] == "copy":
            return {"kind": "copy", "owner": spec["owner"], "source_version": descriptor["source_version"], "bytes_hash": digest(after)}
        state = {"kind": "symlink", "owner": spec["owner"], "source_version": descriptor["source_version"], "bytes_hash": digest(after)}
        state["target"] = str(self.state_dir.parent / "skill-versions" / state["owner"] / str(state["source_version"]) / state["bytes_hash"])
        state["target_hash"] = digest(state["target"].encode())
        self._skill_target(state)
        return state

    def _skill_store(self, state: dict, data: bytes):
        target = self._skill_target(state)
        if target.exists():
            if self._skill_bytes(state) != data:
                raise ConfigurationError("Canonical Skill version does not match")
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._skill_target(state)
        fd, temporary = tempfile.mkstemp(prefix=".skill-version-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o400)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._skill_target(state)
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._skill_bytes(state)
        dirfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        # Retained versions are required by interrupted recovery and bundle
        # baselines; uninstall never deletes canonical versions or link targets.

    def _skill_atomic(self, path: Path, data: bytes | None, state: dict | None, expected: dict | None):
        _, actual = self._skill_read(str(path), [expected])
        if actual != expected:
            raise ConfigurationError("Managed Skill changed during apply")
        if state and state["kind"] == "symlink":
            self._skill_store(state, data)
        if not path.is_symlink() and (not state or state["kind"] == "copy"):
            self._atomic(path, data)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._skill_path(str(path))
        fd, temporary = tempfile.mkstemp(prefix=".senrelay-", dir=path.parent)
        try:
            if state and state["kind"] == "symlink":
                os.close(fd)
                Path(temporary).unlink()
                os.symlink(state["target"], temporary)
            elif data is not None:
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                os.close(fd)
            _, actual = self._skill_read(str(path), [expected])
            if actual != expected:
                raise ConfigurationError("Managed Skill changed during apply")
            if data is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(temporary, path)
            dirfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _item_key(spec: dict) -> str:
        if spec.get("native_identity"):
            return spec["kind"] + ":" + canonical({"container": spec["pointer"][:-1], "identity": spec["native_identity"]})
        return spec["kind"] + ":" + canonical(spec.get("pointer", spec.get("marker", "file")))

    def _owned(self, path: str, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM management_owned WHERE path=? AND item_key=?", (path, key)).fetchone()
        return dict(row) if row else None

    def inspect(self, specs: list[dict]) -> list[dict]:
        """Read only owned entries, without credentials, snapshots or file writes.

        Compare selected entry hashes, rather than the whole file, so an unrelated
        host setting does not turn a correctly installed resource into a conflict.
        Work is bounded even when a host has thousands of managed Skill files.
        """
        if len(specs) > 500:
            raise ConfigurationError("Inspect at most 500 owned entries", 422)
        groups = {}
        for spec in specs:
            groups.setdefault(spec["path"], []).append(spec)
        results, consumed = [], 0
        for path, entries in groups.items():
            try:
                if consumed >= 16 * 1024 * 1024:
                    results.extend({"owner": entry["owner"], "path": path, "status": "not_checked"} for entry in entries)
                    continue
                if entries[0].get("managed_skill"):
                    registered = self._skill_registry(path)
                    raw, state = self._skill_read(path, [registered])
                    if state != registered:
                        results.extend({"owner": entry["owner"], "path": path, "status": "externally_changed"} for entry in entries)
                        continue
                else:
                    raw = self._read(Path(path))
                consumed += len(raw or b"")
                _, inspected = self._render(raw, entries, inspect_only=True)
                results.extend(inspected)
            except (ConfigurationError, OSError, UnicodeError, ValueError, KeyError, TypeError):
                # No raw diagnostics, file content or Keychain access in state.
                results.extend({"owner": entry["owner"], "path": path, "status": "unreadable"} for entry in entries)
        return results

    def _render(self, old: bytes | None, specs: list[dict], resolve: bool = False, *, inspect_only: bool = False) -> tuple[bytes | None, list[dict]]:
        text = (old or b"").decode("utf-8")
        changes = []
        for spec in specs:
            operation, kind = spec.get("operation", "install"), spec["kind"]
            new_value = copy.deepcopy(spec.get("value"))
            if not resolve and spec.get("restore_from") and kind == "block" and spec.get("format") == "toml":
                new_value = "# Previous native block hidden"
            if resolve and spec.get("restore_from"):
                source = spec["restore_from"]
                snapshot = source.get("snapshot")
                raw = None
                if snapshot:
                    snapshot_path = Path(snapshot)
                    if snapshot_path.is_symlink() or not snapshot_path.is_relative_to(self.state_dir):
                        raise ConfigurationError("Invalid activation recovery snapshot")
                    raw = snapshot_path.read_bytes()
                if (digest(raw) if raw is not None else None) != source.get("hash"):
                    raise ConfigurationError("Activation snapshot integrity check failed")
                if source["format"] == "file":
                    present, new_value = raw is not None, raw.decode() if raw is not None else None
                elif source["format"] == "block":
                    marker = source["marker"]
                    match = re.search(re.escape("# BEGIN SENRELAY " + marker) + r"\n(.*?)\n" + re.escape("# END SENRELAY " + marker), raw.decode() if raw else "", re.S)
                    present, new_value = bool(match), match[1] if match else None
                else:
                    if raw and source["format"] in ("yaml", "json5"):
                        from app.integrations.structured_config import parse_document
                        root = parse_document(source["format"], raw.decode())
                    else:
                        root = (tomllib.loads(raw.decode()) if source["format"] == "toml" else json.loads(raw)) if raw else {}
                    source_pointer = list(source["pointer"])
                    if source.get("native_identity"):
                        container = root
                        for component in source_pointer[:-1]:
                            container = container.get(component, []) if isinstance(container, dict) else []
                        matches = [index for index, entry in enumerate(container) if isinstance(entry, dict) and all(entry.get(key) == value for key, value in source["native_identity"].items())] if isinstance(container, list) else []
                        source_pointer[-1] = matches[0] if len(matches) == 1 else -1
                    present = True
                    for key in source_pointer:
                        if isinstance(root, dict) and key in root:
                            root = root[key]
                        elif isinstance(root, list) and type(key) is int and 0 <= key < len(root):
                            root = root[key]
                        else:
                            present = False
                            break
                    new_value = root if present else None
                operation = "install" if present else "uninstall"
            if resolve and operation != "uninstall" and not spec.get("restore_from"):
                def walk(value):
                    if isinstance(value, str):
                        def resolve_reference(match):
                            projection = (spec.get('credential_projection') or {}).get(match[1])
                            if projection and projection.get('expires_at') is not None and projection['expires_at'] <= time.time():
                                raise ConfigurationError('Official account credentials expired after preview; refresh before applying', 409)
                            secret = self.vault.get(match[1])
                            if projection:
                                from app.services.provider_credentials import projected_secret
                                secret = projected_secret(secret, projection)
                            if kind == "block" and spec.get("format") == "toml":
                                # References inside native TOML basic strings
                                # must stay data, never become table syntax.
                                return json.dumps(secret, ensure_ascii=False)[1:-1]
                            return secret
                        return SECRET_REF.sub(resolve_reference, value)
                    if isinstance(value, dict):
                        return {key: walk(val) for key, val in value.items()}
                    if isinstance(value, list):
                        return [walk(val) for val in value]
                    return value
                new_value = walk(new_value)
            exists, current = False, None
            if kind == "json":
                try:
                    data = json.loads(text) if text.strip() else {}
                except json.JSONDecodeError:
                    raise ConfigurationError("JSONC or invalid JSON cannot be safely rewritten; use a manual fragment", 422) from None
                if not isinstance(data, dict):
                    raise ConfigurationError("Configuration root must be an object", 422)
                node = data
                pointer = spec.get("pointer", [])
                if not pointer or any(not isinstance(key, str) or not key for key in pointer):
                    raise ConfigurationError("A non-empty configuration entry path is required", 422)
                if operation == "uninstall":
                    for key in pointer[:-1]:
                        if not isinstance(node, dict) or key not in node:
                            node = None
                            break
                        node = node[key]
                    exists = isinstance(node, dict) and pointer[-1] in node
                    current = node.get(pointer[-1]) if exists else None
                else:
                    for key in pointer[:-1]:
                        if key not in node:
                            node[key] = {}
                        if not isinstance(node[key], dict):
                            raise ConfigurationError("Existing configuration container is not an object")
                        node = node[key]
                    exists = pointer[-1] in node
                    current = node.get(pointer[-1])
            elif kind in ("yaml", "json5"):
                from app.integrations.structured_config import parse_document
                data = parse_document(kind, text)
                pointer = list(spec.get("pointer", []))
                if not pointer:
                    raise ConfigurationError("A native entry pointer is required", 422)
                if spec.get("native_identity"):
                    container = data
                    for component in pointer[:-1]:
                        container = container.get(component, []) if isinstance(container, dict) else []
                    if not isinstance(container, list):
                        raise ConfigurationError("Native provider container must be a list", 422)
                    matches = [index for index, entry in enumerate(container) if isinstance(entry, dict) and all(entry.get(key) == value for key, value in spec["native_identity"].items())]
                    if len(matches) > 1:
                        raise ConfigurationError("Native provider identity is ambiguous", 422)
                    pointer[-1] = matches[0] if matches else len(container)
                node, exists = data, True
                for component in pointer:
                    if isinstance(node, dict) and component in node:
                        node = node[component]
                    elif isinstance(node, list) and type(component) is int and 0 <= component < len(node):
                        node = node[component]
                    else:
                        exists = False
                        break
                current = node if exists else None
            elif kind == "toml_selection":
                try:
                    data = tomllib.loads(text) if text.strip() else {}
                except tomllib.TOMLDecodeError:
                    raise ConfigurationError("Existing TOML is invalid", 422) from None
                pointer = spec.get("pointer", [])
                if len(pointer) != 1 or pointer[0] not in ("model", "model_provider", "default_model"):
                    raise ConfigurationError("Unsupported native TOML selection field", 422)
                exists, current = pointer[0] in data, data.get(pointer[0])
                if exists and not isinstance(current, str):
                    raise ConfigurationError("Native model selection is not a string", 422)
            elif kind == "block":
                marker = spec["marker"]
                start, end = f"# BEGIN SENRELAY {marker}", f"# END SENRELAY {marker}"
                if text.count(start) != text.count(end) or text.count(start) > 1:
                    raise ConfigurationError("Managed markers are malformed or duplicated")
                pattern = re.compile(re.escape(start) + r"\n(.*?)\n" + re.escape(end), re.S)
                match = pattern.search(text)
                exists, current = bool(match), match[1] if match else None
            elif kind == "file":
                exists, current = old is not None, text
            else:
                raise ConfigurationError("Unsupported configuration renderer", 422)
            key = self._item_key(spec)
            owned = self._owned(spec["path"], key)
            current_hash = digest(canonical(current).encode()) if exists else None
            if inspect_only:
                status = "not_managed" if not owned or owned["owner"] != spec["owner"] else "missing" if not exists else "verified" if current_hash == owned["value_hash"] else "externally_changed"
                changes.append({"owner": spec["owner"], "path": spec["path"], "status": status})
                continue
            if operation == "uninstall":
                if not owned and not spec.get("takeover_selected_field"):
                    raise ConfigurationError("Only Senrelay-owned entries may be uninstalled")
                if owned and owned["owner"] != spec["owner"]:
                    raise ConfigurationError("Only Senrelay-owned entries may be uninstalled")
                if owned and exists and current_hash != owned["value_hash"]:
                    raise ConfigurationError("Managed entry was externally edited; refusing uninstall")
            elif exists:
                allowed_owners = {spec["owner"]}
                if spec.get("replaces_owner"):
                    allowed_owners.add(spec["replaces_owner"])
                if owned:
                    if owned["owner"] not in allowed_owners or current_hash != owned["value_hash"]:
                        raise ConfigurationError("Target entry already exists or was externally edited")
                elif not spec.get("takeover_selected_field"):
                    raise ConfigurationError("Target entry already exists or was externally edited")
            if kind == "json":
                if operation == "uninstall":
                    if node is not None and isinstance(node, dict):
                        node.pop(pointer[-1], None)
                else:
                    node[pointer[-1]] = new_value
                text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            elif kind in ("yaml", "json5"):
                from app.integrations.structured_config import render_document, parse_document
                edits = []
                if operation != "uninstall":
                    edits.extend({"pointer": pointer + [field], "operation": "uninstall"} for field in spec.get("remove_fields", []))
                edits.append({"pointer": pointer, "value": new_value, "operation": "merge" if spec.get("merge") and operation != "uninstall" else operation})
                text = render_document(kind, text, edits)
                if operation != "uninstall":
                    actual = parse_document(kind, text)
                    for component in pointer:
                        actual = actual[component]
                    new_value = actual
            elif kind == "toml_selection":
                field_key = pointer[0]
                table = re.search(r"(?m)^\s*\[", text)
                head, tail = (text[:table.start()], text[table.start():]) if table else (text, "")
                line = re.compile(r"(?m)^[ \t]*(?:" + re.escape(field_key) + r"|\"" + re.escape(field_key) + r"\"|'" + re.escape(field_key) + r"')\s*=([^\n]*)(?:\n|$)")
                matches = list(line.finditer(head))
                if exists and len(matches) != 1:
                    raise ConfigurationError("Native TOML selection cannot be safely located", 422)
                comment = ""
                if matches:
                    old_assignment = matches[0][1]
                    if ('"' * 3) in old_assignment or ("'" * 3) in old_assignment:
                        raise ConfigurationError("Multiline TOML model selections require manual review", 422)
                    comment = toml_inline_comment(old_assignment)
                replacement = (comment + "\n" if comment else "") if operation == "uninstall" else field_key + " = " + json.dumps(new_value, ensure_ascii=False) + (" " + comment if comment else "") + "\n"
                head = line.sub(lambda _: replacement, head) if matches else replacement + head
                text = head + tail
                try:
                    tomllib.loads(text)
                except tomllib.TOMLDecodeError:
                    raise ConfigurationError("Native TOML selection failed validation", 422) from None
            elif kind == "block":
                replacement = "" if operation == "uninstall" else f"{start}\n{new_value}\n{end}"
                if exists:
                    text = pattern.sub(lambda _: replacement, text)
                elif operation != "uninstall":
                    text = text.rstrip("\n") + ("\n\n" if text else "") + replacement + "\n"
                if spec.get("format") == "toml":
                    try:
                        tomllib.loads(text)
                    except tomllib.TOMLDecodeError:
                        raise ConfigurationError("TOML fragment conflicts with existing configuration", 422) from None
            else:
                text = "" if operation == "uninstall" else str(new_value)
            changes.append({"path": spec["path"], "key": key, "owner": spec.get("restore_owner", spec["owner"]),
                            "operation": "uninstall" if spec.get("relinquish") else operation,
                            "value_hash": digest(canonical(new_value).encode()) if operation != "uninstall" else None})
        if inspect_only:
            return old, changes
        if specs[0]["kind"] == "file" and changes[-1]["operation"] == "uninstall" and not specs[-1].get("relinquish"):
            return None, changes
        if specs[0]["kind"] == "file" and operation == "uninstall":
            return None, changes
        return text.encode(), changes

    def create(self, specs: list[dict], metadata: dict | None = None) -> dict:
        if not specs or len(specs) > 200:
            raise ConfigurationError("Plan must contain between 1 and 200 entries", 422)
        files = {}
        for raw in specs:
            spec = copy.deepcopy(raw)
            reject_secrets(spec.get("value"))
            if spec.get("managed_skill"):
                descriptor = spec["managed_skill"]
                if not isinstance(descriptor, dict) or set(descriptor) != {"mode", "source_version"} or spec["kind"] != "file" or not re.fullmatch(r"asset_[a-f0-9]{16}", spec.get("owner", "")) or descriptor["mode"] not in ("copy", "symlink"):
                    raise ConfigurationError("Invalid typed managed Skill operation", 422)
                version = descriptor["source_version"]
                if (type(version) is not int or version < 1) and not (version is None and spec.get("restore_from")):
                    raise ConfigurationError("Managed Skill source version is required", 422)
            path = str(self._skill_path(spec["path"]) if spec.get("managed_skill") else self.safe_path(spec["path"]))
            spec["path"] = path
            spec["original_ownership"] = self._owned(path, self._item_key(spec))
            files.setdefault(path, []).append(spec)
        payload = {"files": [], "metadata": metadata or {}, "readback": "not_run", "host_reload": "not_run", "real_request": "not_run"}
        for path, entries in files.items():
            skill = entries[0].get("managed_skill")
            if skill and len(entries) != 1:
                raise ConfigurationError("A managed Skill file requires one operation", 422)
            registered = self._skill_registry(path) if skill else None
            if skill:
                old, before_state = self._skill_read(path, [registered])
                if registered is not None and registered != before_state:
                    raise ConfigurationError("Managed Skill file was externally edited")
            else:
                old, before_state = self._read(Path(path)), None
            after, _ = self._render(old, entries, resolve=bool(skill and entries[0].get("restore_from")))
            after_state = self._skill_state(entries[0], after) if skill else None
            if after_state and after_state["kind"] == "symlink" and self._skill_target(after_state).exists():
                self._skill_bytes(after_state)
            # Never serialize original host content into the plan database.
            # Unknown fields and native credential env names are opaque even
            # when no recognisable token prefix exists. Only the user's new
            # managed values are eligible for a redacted preview.
            before_text = "[Existing selected entries hidden]" if old is not None else "[No existing file]"
            after_text = "\n".join(canonical({
                "operation": entry.get("operation", "install"),
                "entry": entry.get("pointer", entry.get("marker", "managed file")),
                "value": ({"install_mode": skill["mode"], "source_version": skill["source_version"], "before": before_state, "after": after_state, "content": redact(entry.get("value"))} if skill else redact(entry.get("value"))) if not any(SENSITIVE.search(str(key)) for key in entry.get("pointer", [])) else "[REDACTED]",
            }) for entry in entries)
            payload["files"].append({"path": path, "before_hash": digest(old) if old is not None else None,
                                     "specs": entries, "stage": "pending", "after_hash": digest(after) if after is not None else None,
                                     **({"before_state": before_state, "after_state": after_state,
                                         "before_registry": registered,
                                         "install_mode": skill["mode"], "source_version": skill["source_version"],
                                         "retained_versions": "Required for recovery and bundle baselines; not removed on uninstall"} if skill else {}),
                                     "diff": "\n".join(difflib.unified_diff(before_text.splitlines(), after_text.splitlines(), fromfile="current", tofile="planned", lineterm=""))})
        plan_id = "plan_" + uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("INSERT INTO management_plans VALUES(?,1,'planned',?,?,?)", (plan_id, canonical(payload), now, now))
        return self.get(plan_id)

    def create_deferred(self, paths: list[str], metadata: dict) -> dict:
        """Reserve a connection plan without persisting host contents or credentials.

        The trusted connection service renders bytes only at apply time. It uses
        this same transaction journal, snapshot, readback and recovery machinery.
        """
        if metadata.get("namespace") != "connections" or not paths or len(paths) > MAX_CONNECTION_FILES or len(set(paths)) != len(paths):
            raise ConfigurationError("Invalid connection plan", 422)
        normalized = [self.safe_path(raw) for raw in paths]
        if len(set(normalized)) != len(normalized):
            raise ConfigurationError('Duplicate connection target after path normalization', 422)
        files = []
        for path in normalized:
            old = self._read(path)
            files.append({"path": str(path), "before_hash": digest(old) if old is not None else None,
                          "before_mode": stat.S_IMODE(path.stat().st_mode) if old is not None else None,
                          "stage": "pending", "specs": [], "after_hash": None})
        payload = {"files": files, "metadata": metadata, "renderer": "connection_components_v1",
                   "readback": "not_run", "host_reload": "not_run", "real_request": "not_run"}
        plan_id, now = "plan_" + uuid4().hex, datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("INSERT INTO management_plans VALUES(?,1,'planned',?,?,?)", (plan_id, canonical(payload), now, now))
        return self.get(plan_id)

    def get(self, plan_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM management_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise ConfigurationError("Configuration plan was not found", 404)
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        # Specs contain credential references, not plaintext. They are kept out
        # of normal API responses in favour of a redacted reviewable diff.
        return data

    def list(self) -> list[dict]:
        with self.connect() as conn:
            ids = conn.execute("SELECT id FROM management_plans ORDER BY created_at DESC LIMIT 200").fetchall()
        return [self.get(row[0]) for row in ids]

    def list_page(self, host_id=None, limit=50, offset=0) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ConfigurationError('Use a page size of 1–100 and a nonnegative offset', 422)
        where, params = (" WHERE json_extract(payload,'$.metadata.host_id')=?", [host_id]) if host_id else ('', [])
        with self.connect() as conn:
            conn.execute('BEGIN')
            total = conn.execute('SELECT count(*) FROM management_plans' + where, params).fetchone()[0]
            rows = conn.execute("SELECT id,version,status,created_at,updated_at,json_extract(payload,'$.metadata.host_id') host_id,json_extract(payload,'$.metadata.operation') operation FROM management_plans" + where + ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
        items = [{**{key: row[key] for key in ('id', 'version', 'status', 'created_at', 'updated_at')},
                  'payload': {'metadata': {'host_id': row['host_id'], 'operation': row['operation']}}, '_summary': True} for row in rows]
        return {'items': items, 'total': total, 'offset': offset, 'limit': limit,
                'has_more': offset + len(items) < total, 'summary': True, 'agent_id': host_id}

    def _save(self, plan: dict, status: str):
        with self.connect() as conn:
            conn.execute("UPDATE management_plans SET status=?,payload=?,updated_at=? WHERE id=?", (status, canonical(plan["payload"]), datetime.now(timezone.utc).isoformat(), plan["id"]))

    def _atomic(self, path: Path, data: bytes | None):
        self.safe_path(str(path))
        if data is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.safe_path(str(path))
        fd, tmp = tempfile.mkstemp(prefix=".senrelay-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            self.safe_path(str(path))
            os.replace(tmp, path)
            dirfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def apply(
        self,
        plan_id: str,
        expected_version: int,
        confirmed: bool,
        finalize: Callable[[sqlite3.Connection, dict[str, Any]], None] | None = None,
        renderer: Callable[[dict], dict[str, bytes | None]] | None = None,
    ) -> dict:
        if confirmed is not True or type(expected_version) is not int:
            raise ConfigurationError("Explicit confirmation and an integer plan version are required", 422)
        with _LOCK:
            plan = self.get(plan_id)
            deferred = plan["payload"].get("renderer") == "connection_components_v1"
            if deferred and (renderer is None or finalize is None):
                raise ConfigurationError("Use the connection plan endpoint to apply this plan")
            if plan["version"] != expected_version:
                raise ConfigurationError("Plan version changed")
            if plan["status"] == "applied":
                return plan
            if plan["status"] != "planned":
                raise ConfigurationError("Plan is not applicable; inspect or recover it first")
            self.require_fresh(plan)
            if deferred:
                for item in plan['payload']['files']:
                    current = self._read(Path(item['path']))
                    mode = stat.S_IMODE(Path(item['path']).stat().st_mode) if current is not None else None
                    if (digest(current) if current is not None else None) != item['before_hash'] or mode != item['before_mode']:
                        self._save(plan, 'conflict')
                        raise ConfigurationError('Configuration changed since preview; create a new plan')
            rendered = renderer(plan) if deferred else None
            if deferred and set(rendered) != {item["path"] for item in plan["payload"]["files"]}:
                raise ConfigurationError("Connection plan targets changed; create a new preview")
            prepared = []
            # Preflight every file and every secret before the first mutation.
            for item in plan["payload"]["files"]:
                if "before_state" in item:
                    if self._skill_registry(item["path"]) != item["before_registry"] or self._owned(item["path"], self._item_key(item["specs"][0])) != item["specs"][0]["original_ownership"]:
                        raise ConfigurationError("Managed Skill ownership changed since preview")
                    old, current_state = self._skill_read(item["path"], [item["before_state"]])
                    if current_state != item["before_state"]:
                        raise ConfigurationError("Managed Skill changed since preview")
                else:
                    old = self._read(Path(item["path"]))
                if (digest(old) if old is not None else None) != item["before_hash"]:
                    self._save(plan, "conflict")
                    raise ConfigurationError("Configuration changed since preview; create a new plan")
                if deferred:
                    mode = stat.S_IMODE(Path(item['path']).stat().st_mode) if old is not None else None
                    if mode != item['before_mode']:
                        raise ConfigurationError("Configuration permissions changed since preview")
                    after, changes = rendered[item["path"]], []
                else:
                    after, changes = self._render(old, item["specs"], resolve=True)
                if "before_state" in item and self._skill_state(item["specs"][0], after) != item["after_state"]:
                    raise ConfigurationError("Managed Skill restore changed since preview")
                state = item.get("after_state")
                if state and state["kind"] == "symlink" and self._skill_target(state).exists():
                    self._skill_bytes(state)
                prepared.append((item, old, after, changes))
            if any(part.is_symlink() for part in [self.state_dir, *self.state_dir.parents]):
                raise ConfigurationError("Snapshot directory cannot contain symlinks")
            from app.private_storage import private_directory
            private_directory(self.state_dir)
            with self.connect() as conn:
                claimed = conn.execute("UPDATE management_plans SET status='applying' WHERE id=? AND status='planned'", (plan_id,))
                if claimed.rowcount != 1:
                    raise ConfigurationError("Another operation already claimed this plan")
            try:
                for index, (item, old, after, changes) in enumerate(prepared):
                    backup = self.state_dir / f"{plan_id}-{index}.snapshot"
                    if old is not None:
                        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as file:
                            file.write(old)
                            file.flush()
                            os.fsync(file.fileno())
                    item.update(snapshot=str(backup) if old is not None else None,
                                after_hash=digest(after) if after is not None else None,
                                changes=changes, stage="intent")
                    self._save(plan, "applying")
                    if "before_state" in item:
                        self._skill_atomic(Path(item["path"]), after, item["after_state"], item["before_state"])
                        actual, actual_state = self._skill_read(item["path"], [item["after_state"]])
                        if actual_state != item["after_state"]:
                            raise ConfigurationError("Managed Skill link readback did not match")
                    else:
                        current = self._read(Path(item["path"]))
                        if (digest(current) if current is not None else None) != item["before_hash"]:
                            raise ConfigurationError("Configuration changed during apply")
                        if current != after:
                            self._atomic(Path(item["path"]), after)
                        actual = self._read(Path(item["path"]))
                    if actual != after:
                        raise ConfigurationError("Configuration readback did not match")
                    item["stage"] = "verified"
                    self._save(plan, "applying")
                with self.connect() as conn:
                    for item, _, _, changes in prepared:
                        if "before_state" in item:
                            conn.execute("DELETE FROM management_skill_files WHERE path=?", (item["path"],))
                            if item["after_state"] is not None and not item["specs"][0].get("relinquish"):
                                conn.execute("INSERT INTO management_skill_files VALUES(?,?)", (item["path"], canonical(item["after_state"])))
                        for change in changes:
                            if change["operation"] == "uninstall":
                                conn.execute("DELETE FROM management_owned WHERE path=? AND item_key=?", (change["path"], change["key"]))
                            else:
                                conn.execute("INSERT OR REPLACE INTO management_owned VALUES(?,?,?,?)", (change["path"], change["key"], change["value_hash"], change["owner"]))
                    if finalize is not None:
                        finalize(conn, plan)
                    plan["payload"]["readback"] = "verified"
                    conn.execute("UPDATE management_plans SET status='applied',payload=? WHERE id=?", (canonical(plan["payload"]), plan_id))
            except Exception as failure:
                plan['payload']['failure'] = {
                    'code': 'filesystem_error' if isinstance(failure, OSError) else
                            'database_error' if isinstance(failure, sqlite3.Error) else
                            'configuration_validation_failed' if isinstance(failure, ConfigurationError) else
                            'apply_interrupted',
                    'occurred_at': datetime.now(timezone.utc).isoformat(),
                    'recovery_required': True,
                }
                self._save(plan, "recovery_required")
                # No exception text, command output or secret is persisted.
                raise ConfigurationError("Apply was interrupted; configuration recovery is required") from None
            return self.get(plan_id)

    @staticmethod
    def require_fresh(plan: dict) -> None:
        if plan['status'] == 'applied':
            return
        try:
            created = datetime.fromisoformat(plan['created_at'])
            age = (datetime.now(timezone.utc) - created).total_seconds()
        except (KeyError, TypeError, ValueError):
            raise ConfigurationError('Plan timestamp is invalid; create a new preview', 409) from None
        if age < 0 or age >= 30 * 60:
            raise ConfigurationError('Plan expired; create a new preview (valid for 30 minutes)', 409)

    def recover(self, plan_id: str, confirmed: bool) -> dict:
        if confirmed is not True:
            raise ConfigurationError("Explicit recovery confirmation is required", 422)
        with _LOCK:
            plan = self.get(plan_id)
            if plan["status"] == "recovered":
                return plan
            if plan["status"] not in ("recovery_required", "applying"):
                raise ConfigurationError("Only interrupted plans may be recovered")
            # Verify all targets before touching any; never overwrite edits
            # made after interruption. Recovery is safely repeatable.
            for item in plan["payload"]["files"]:
                if item["stage"] not in ("intent", "verified"):
                    continue
                for spec in item['specs']:
                    if self._owned(item['path'], self._item_key(spec)) != spec.get('original_ownership'):
                        raise ConfigurationError('Recovery stopped: configuration ownership changed')
                if "before_state" in item:
                    if self._skill_registry(item["path"]) != item["before_registry"] or self._owned(item["path"], self._item_key(item["specs"][0])) != item["specs"][0]["original_ownership"]:
                        raise ConfigurationError("Recovery stopped: managed Skill ownership changed")
                    before_state = item["before_state"]
                    if before_state and before_state["kind"] == "symlink":
                        self._skill_bytes(before_state)
                    current, state = self._skill_read(item["path"], [item["before_state"], item["after_state"]])
                    if state not in (item["before_state"], item["after_state"]):
                        raise ConfigurationError("Recovery stopped: managed Skill was externally edited")
                else:
                    current = self._read(Path(item["path"]))
                current_hash = digest(current) if current is not None else None
                if current_hash not in (item["before_hash"], item["after_hash"]):
                    raise ConfigurationError("Recovery stopped: target was externally edited")
            for item in reversed(plan["payload"]["files"]):
                if item["stage"] not in ("intent", "verified"):
                    continue
                snapshot = item.get("snapshot")
                old = None
                if snapshot:
                    snapshot_path = Path(snapshot)
                    if snapshot_path.is_symlink() or not snapshot_path.is_relative_to(self.state_dir):
                        raise ConfigurationError("Invalid activation recovery snapshot")
                    old = snapshot_path.read_bytes()
                if (digest(old) if old is not None else None) != item["before_hash"]:
                    raise ConfigurationError("Recovery snapshot failed integrity validation")
                if "before_state" in item:
                    _, current_state = self._skill_read(item["path"], [item["before_state"], item["after_state"]])
                    self._skill_atomic(Path(item["path"]), old, item["before_state"], current_state)
                    actual, restored_state = self._skill_read(item["path"], [item["before_state"]])
                    if restored_state != item["before_state"]:
                        raise ConfigurationError("Recovery link readback failed")
                else:
                    self._atomic(Path(item["path"]), old)
                    actual = self._read(Path(item["path"]))
                if actual != old:
                    raise ConfigurationError("Recovery readback failed")
                item["stage"] = "restored"
                self._save(plan, "recovery_required")
            plan["payload"]["readback"] = "restored"
            self._save(plan, "recovered")
            return self.get(plan_id)
