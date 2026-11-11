"""Comment-preserving optional YAML/JSON5 native configuration transformations.

Pure text I/O: ownership, expected hashes, secret resolution, writes and rollback
belong to ConfigurationTransactions. No host, model, network or file is accessed.
YAML uses ruamel round-trip nodes; JSON5 uses json-five's concrete model. A plain
JSON5 loads/dumps round trip is deliberately never used to render documents.
"""
from __future__ import annotations

import copy
import io
import json
import math
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime
from importlib import import_module
from typing import Any

from app.integrations.config_transactions import ConfigurationError

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_OPERATIONS = 256
SET_OPERATIONS = {"install", "set", "replace", "upsert", "update"}
REMOVE_OPERATIONS = {"remove", "uninstall", "delete"}


class StructuredFormatUnavailable(ConfigurationError):
    def __init__(self, format: str):
        super().__init__(f"Native {format} format runtime unavailable; install requirements-managed-formats.txt", 503)
        self.code = "format_runtime_unavailable"
        self.format = format


def _runtime(format: str):
    try:
        if format == "yaml":
            return import_module("ruamel.yaml")
        if format == "json5":
            module = import_module("json5")
            # A different PyPI package uses the same import name without a model.
            import_module("json5.model")
            import_module("json5.dumper").ModelDumper
            return module
    except (ImportError, AttributeError) as exc:
        raise StructuredFormatUnavailable(format) from exc
    raise ConfigurationError("Structured formats are yaml and json5", 422)


def runtime_status(format: str) -> dict:
    try:
        _runtime(format)
        return {"format": format, "status": "available", "host_loaded": "not_verified"}
    except StructuredFormatUnavailable:
        return {"format": format, "status": "runtime_unavailable", "host_loaded": "not_verified"}


def _bounded(text: str) -> None:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ConfigurationError("Native document must be UTF-8 text under 4 MiB", 422)


def _validate_value(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ConfigurationError("Native configuration value exceeds nesting limit", 422)
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for child in value:
            _validate_value(child, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for child in value.values():
            _validate_value(child, depth + 1)
        return
    raise ConfigurationError("Native operation values must contain only finite JSON-compatible data", 422)


def _operations(operations: list[dict]) -> list[dict]:
    if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
        raise ConfigurationError("Supply at most 256 native configuration operations", 422)
    result = []
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) - {"pointer", "value", "operation"}:
            raise ConfigurationError("Unsupported native operation fields", 422)
        pointer = operation.get("pointer")
        action = operation.get("operation", "install")
        if not isinstance(pointer, list) or not pointer or len(pointer) > 32 or any(
            isinstance(key, bool) or not isinstance(key, (str, int)) or isinstance(key, str) and not key or isinstance(key, int) and key < 0
            for key in pointer
        ):
            raise ConfigurationError("A bounded nonempty native pointer is required", 422)
        if action not in SET_OPERATIONS | REMOVE_OPERATIONS | {"merge"}:
            raise ConfigurationError("Unsupported native operation", 422)
        if action not in REMOVE_OPERATIONS:
            if "value" not in operation:
                raise ConfigurationError("Native set operation requires a value", 422)
            _validate_value(operation["value"])
            if len(json.dumps(operation["value"], ensure_ascii=False).encode()) > MAX_DOCUMENT_BYTES:
                raise ConfigurationError("Native operation value exceeds limit", 422)
        # Operations are intentionally sequential, like JSON Patch: explicit
        # child removal followed by parent merge can preserve native extras.
        result.append(copy.deepcopy(operation))
    return result


def _yaml_runtime():
    module = _runtime("yaml")
    yaml = module.YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = False
    yaml.allow_unicode = True
    yaml.width = 4096
    return yaml


def _yaml_load(text: str):
    yaml = _yaml_runtime()
    try:
        from ruamel.yaml.events import CollectionStartEvent, CollectionEndEvent, AliasEvent
        # The RT constructor can silently substitute None for a recursive alias.
        # Detect cycles before construction so untouched foreign data cannot change.
        stack, active = [], set()
        for event in yaml.parse(text if text.strip() else "{}\n"):
            if isinstance(event, CollectionStartEvent):
                stack.append(event.anchor)
                if event.anchor:
                    active.add(event.anchor)
            elif isinstance(event, CollectionEndEvent):
                anchor = stack.pop()
                if anchor:
                    active.discard(anchor)
            elif isinstance(event, AliasEvent) and event.anchor in active:
                raise ConfigurationError("Recursive YAML aliases cannot be safely edited", 422)
        value = yaml.load(text if text.strip() else "{}\n")
    except Exception as exc:
        raise ConfigurationError("Invalid or ambiguous native YAML document", 422) from exc
    if value is None:
        value = yaml.load("{}\n")
    if not isinstance(value, Mapping):
        raise ConfigurationError("Native configuration root must be an object", 422)
    _yaml_counts(value)  # also rejects recursive aliases and non-data tags
    return yaml, value


def _yaml_counts(root) -> Counter:
    counts = Counter()
    def visit(value, stack):
        if hasattr(value, "tag") and str(value.tag) not in {"None", "tag:yaml.org,2002:map", "tag:yaml.org,2002:seq", "tag:yaml.org,2002:str"}:
            raise ConfigurationError("Custom YAML tags are not native configuration data", 422)
        if isinstance(value, (Mapping, list)):
            identity = id(value)
            counts[identity] += 1
            if identity in stack:
                raise ConfigurationError("Recursive YAML aliases cannot be safely edited", 422)
            if counts[identity] > 1:
                return
            children = value.values() if isinstance(value, Mapping) else value
            for child in children:
                visit(child, stack | {identity})
    visit(root, set())
    return counts


def _plain(value):
    budget = [0]
    def convert(node, depth):
        budget[0] += 1
        if budget[0] > 100000 or depth > 64:
            raise ConfigurationError("Native document expansion exceeds safety limits", 422)
        if isinstance(node, Mapping):
            return {key: convert(child, depth + 1) for key, child in node.items()}
        if isinstance(node, list):
            return [convert(child, depth + 1) for child in node]
        if isinstance(node, (datetime, date)):
            return node.isoformat()
        return node
    return convert(value, 0)


def _yaml_detach(value):
    result = copy.deepcopy(value)
    def clear(node):
        if hasattr(node, "yaml_set_anchor"):
            node.yaml_set_anchor(None)
        if isinstance(node, Mapping):
            for child in node.values():
                clear(child)
        elif isinstance(node, list):
            for child in node:
                clear(child)
    clear(result)
    return result


def _mapping_get(node, key):
    if isinstance(node, Mapping) and isinstance(key, str):
        return key in node, node.get(key)
    if isinstance(node, list) and isinstance(key, int):
        return key < len(node), node[key] if key < len(node) else None
    raise ConfigurationError("Native pointer does not match the existing container type", 422)


def _yaml_parent(root, pointer, create: bool):
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    node = root
    counts = _yaml_counts(root)
    for index, key in enumerate(pointer[:-1]):
        exists, child = _mapping_get(node, key)
        if not exists:
            if not create:
                return None
            child = CommentedSeq() if isinstance(pointer[index + 1], int) else CommentedMap()
            if isinstance(node, list):
                if key != len(node):
                    raise ConfigurationError("Native array pointers cannot create sparse arrays", 422)
                node.append(child)
            else:
                node[key] = child
        elif isinstance(child, (Mapping, list)) and counts[id(child)] > 1:
            # Modifying an alias must not mutate a different unselected field.
            child = _yaml_detach(child)
            node[key] = child
        if not isinstance(child, (Mapping, list)):
            raise ConfigurationError("Native pointer would replace a non-container ancestor", 422)
        node = child
    return node


def _merge_mapping(existing, incoming):
    for key, value in incoming.items():
        if key in existing and isinstance(existing[key], Mapping) and isinstance(value, dict):
            # Detach even if shared through a YAML alias or merge-key source.
            existing[key] = _yaml_detach(existing[key])
            _merge_mapping(existing[key], value)
        else:
            existing[key] = copy.deepcopy(value)


def _render_yaml(text: str, operations: list[dict]) -> str:
    yaml, root = _yaml_load(text)
    changed = False
    for operation in operations:
        action, pointer = operation.get("operation", "install"), operation["pointer"]
        node = _yaml_parent(root, pointer, action not in REMOVE_OPERATIONS)
        if node is None:
            continue
        key = pointer[-1]
        exists, current = _mapping_get(node, key)
        if action in REMOVE_OPERATIONS:
            if exists:
                del node[key]
                changed = True
            continue
        value = operation["value"]
        if action == "merge" and exists:
            if not isinstance(current, Mapping) or not isinstance(value, dict):
                raise ConfigurationError("Native merge requires object values", 422)
            original = _plain(current)
            node[key] = _yaml_detach(current)
            _merge_mapping(node[key], value)
            changed |= original != _plain(node[key])
        elif not exists or _plain(current) != value:
            if isinstance(node, list) and key == len(node):
                node.append(copy.deepcopy(value))
            elif isinstance(node, list) and key > len(node):
                raise ConfigurationError("Native array pointers cannot create sparse arrays", 422)
            else:
                node[key] = copy.deepcopy(value)
            changed = True
    if not changed:
        return text
    yaml.line_break = "\r\n" if "\r\n" in text else "\n"
    output = io.StringIO()
    yaml.dump(root, output)
    rendered = output.getvalue()
    _yaml_load(rendered)
    return rendered


def _json5_load(text: str):
    module = _runtime("json5")
    from json5.loader import ModelLoader, DefaultLoader
    from json5.model import JSONObject, JSONArray
    try:
        document = module.loads(text if text.strip() else "{}", loader=ModelLoader())
    except Exception as exc:
        raise ConfigurationError("Invalid native JSON5 document", 422) from exc
    def validate(node, depth=0):
        if depth > 64:
            raise ConfigurationError("JSON5 document exceeds nesting limit", 422)
        if isinstance(node, JSONObject):
            names = [str(DefaultLoader().load(key)) for key in node.keys]
            if len(set(names)) != len(names):
                raise ConfigurationError("Duplicate native JSON5 keys cannot be safely edited", 422)
            for child in node.values:
                validate(child, depth + 1)
        elif isinstance(node, JSONArray):
            for child in node.values:
                validate(child, depth + 1)
    if not isinstance(document.value, JSONObject):
        raise ConfigurationError("Native configuration root must be an object", 422)
    validate(document.value)
    return module, document


def _json5_value(value):
    from json5.loader import ModelLoader
    module = _runtime("json5")
    # Strict JSON serialization of NEW data guarantees strings stay data. Never
    # use repr-based modelization of credentials or user-controlled field values.
    return module.loads(json.dumps(value, ensure_ascii=False, allow_nan=False), loader=ModelLoader()).value


def _json5_get(node, key):
    from json5.model import JSONObject, JSONArray
    from json5.loader import DefaultLoader
    if isinstance(node, JSONObject) and isinstance(key, str):
        for index, old in enumerate(node.keys):
            if str(DefaultLoader().load(old)) == key:
                return index, node.values[index]
        return None, None
    if isinstance(node, JSONArray) and isinstance(key, int):
        return (key, node.values[key]) if key < len(node.values) else (None, None)
    raise ConfigurationError("Native pointer does not match the existing container type", 422)


def _json5_put(node, key, value):
    from json5.model import JSONObject, JSONArray
    position, old = _json5_get(node, key)
    if position is not None:
        value.wsc_before = old.wsc_before
        value.wsc_after = old.wsc_after
        node.values[position] = value
    elif isinstance(node, JSONArray):
        if key != len(node.values):
            raise ConfigurationError("Native array pointers cannot create sparse arrays", 422)
        node.values.append(value)
    else:
        new_key = _json5_value(key)
        # Keep the old final entry's inline comments before the newly appended key.
        if node.trailing_comma:
            new_key.wsc_before = node.trailing_comma.wsc_after
            node.trailing_comma.wsc_after = ["\n"] if any(isinstance(s, str) and "\n" in s for s in new_key.wsc_before) else []
        elif node.values:
            new_key.wsc_before = node.values[-1].wsc_after
            node.values[-1].wsc_after = []
        node.keys.append(new_key)
        node.values.append(value)


def _json5_remove(node, key):
    from json5.model import JSONObject, JSONArray
    position, old = _json5_get(node, key)
    if position is None:
        return False
    if isinstance(node, JSONObject):
        old_key = node.keys.pop(position)
        preserved = old_key.wsc_before + old_key.wsc_after + old.wsc_before + old.wsc_after
        if position < len(node.keys):
            node.keys[position].wsc_before = preserved + node.keys[position].wsc_before
        elif node.trailing_comma:
            node.trailing_comma.wsc_after = preserved + node.trailing_comma.wsc_after
        elif position:
            node.values[position - 1].wsc_after += preserved
        else:
            node.leading_wsc += preserved
    else:
        preserved = old.wsc_before + old.wsc_after
        if position + 1 < len(node.values):
            node.values[position + 1].wsc_before = preserved + node.values[position + 1].wsc_before
        else:
            node.leading_wsc += preserved
    node.values.pop(position)
    if not node.values and node.trailing_comma:
        node.leading_wsc += node.trailing_comma.wsc_before + node.trailing_comma.wsc_after
        node.trailing_comma = None
    return True


def _json5_merge(node, value):
    from json5.model import JSONObject
    if not isinstance(node, JSONObject) or not isinstance(value, dict):
        raise ConfigurationError("Native merge requires object values", 422)
    for key, child in value.items():
        _, old = _json5_get(node, key)
        if isinstance(old, JSONObject) and isinstance(child, dict):
            _json5_merge(old, child)
        else:
            _json5_put(node, key, _json5_value(child))


def _render_json5(text: str, operations: list[dict]) -> str:
    from json5.loader import DefaultLoader
    from json5.dumper import ModelDumper
    from json5.model import JSONObject, JSONArray
    module, document = _json5_load(text)
    changed = False
    for operation in operations:
        action, pointer = operation.get("operation", "install"), operation["pointer"]
        node = document.value
        missing = False
        for index, key in enumerate(pointer[:-1]):
            _, child = _json5_get(node, key)
            if child is None:
                if action in REMOVE_OPERATIONS:
                    missing = True
                    break
                child = _json5_value([] if isinstance(pointer[index + 1], int) else {})
                _json5_put(node, key, child)
            if not isinstance(child, (JSONObject, JSONArray)):
                raise ConfigurationError("Native pointer would replace a non-container ancestor", 422)
            node = child
        if missing:
            continue
        key = pointer[-1]
        if action in REMOVE_OPERATIONS:
            changed |= _json5_remove(node, key)
            continue
        _, old = _json5_get(node, key)
        value = operation["value"]
        before = DefaultLoader().load(old) if old is not None else None
        if action == "merge" and old is not None:
            _json5_merge(old, value)
            changed |= before != DefaultLoader().load(old)
        elif old is None or before != value:
            _json5_put(node, key, _json5_value(value))
            changed = True
    if not changed:
        return text
    result = module.dumps(document, dumper=ModelDumper())
    _json5_load(result)
    return result


def parse_document(format: str, text: str) -> dict:
    """Plain values for ownership hashing only; rendering uses original RT nodes."""
    _bounded(text)
    _runtime(format)
    if format == "yaml":
        return _plain(_yaml_load(text)[1])
    from json5.loader import DefaultLoader
    return DefaultLoader().load(_json5_load(text)[1])


def render_document(format: str, originaltext: str, operations: list[dict]) -> str:
    _bounded(originaltext)
    _runtime(format)
    actions = _operations(operations)
    result = _render_yaml(originaltext, actions) if format == "yaml" else _render_json5(originaltext, actions)
    _bounded(result)
    return result
