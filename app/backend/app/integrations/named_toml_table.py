"""Edit one explicitly declared TOML table, verifying the full document after editing."""
from __future__ import annotations

import copy
import json
import re
import tomllib

from app.integrations.config_transactions import ConfigurationError


def _toml(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return "{ " + ", ".join(f"{_toml(k)} = {_toml(v)}" for k, v in value.items()) + " }"
    raise ConfigurationError("Native TOML values must be bounded strings, integers, booleans, lists or maps", 422)


def _table(parts: list[str], fields: dict) -> str:
    return "[" + ".".join(_toml(p) for p in parts) + "]\n" + "\n".join(f"{_toml(key)} = {_toml(value)}" for key, value in fields.items())


def document(text):
    try:
        return tomllib.loads(text)
    except (ValueError, RecursionError):
        raise ConfigurationError('Native TOML could not be parsed', 422) from None


def replace(text, pointer, value):
    root, name = pointer
    before = document(text)
    container = before.get(root, {})
    if not isinstance(container, dict):
        raise ConfigurationError('Expected a named native TOML table', 422)
    if value is not None and not isinstance(value, dict):
        raise ConfigurationError('Native TOML entry must be a table', 422)
    if name in container and container[name] == value:
        return text

    intervals = []
    for match in re.finditer(r'(?m)^[ \t]*\[.*$', text):
        try:
            section = tomllib.loads(match.group())
        except ValueError:
            continue
        intervals.append((match.start(), section))
    selected = [i for i, (_, section) in enumerate(intervals) if section == {root:{name:{}}}]
    if name in container and len(selected) != 1:
        raise ConfigurationError('This native TOML shape requires its original editor', 409)
    if selected:
        idx = selected[0]
        start = intervals[idx][0]
        end = intervals[idx+1][0] if idx+1 < len(intervals) else len(text)
        lines = text[start:end].splitlines(keepends=True)
        while len(lines) > 1 and (not lines[-1].strip() or lines[-1].lstrip().startswith('#')):
            end -= len(lines.pop())
    else:
        start = end = len(text)
    replacement = (_table(pointer, value) + '\n') if value is not None else ''
    if replacement and start and text[start-1] != '\n':
        replacement = '\n' + replacement
    updated = text[:start] + replacement + text[end:]
    # A header-looking line inside a multiline literal, a dotted key or a
    # nested table must never silently consume its neighbour or change it.
    result = document(updated)
    expected = copy.deepcopy(before)
    changed = expected.setdefault(root, {})
    if value is None:
        changed.pop(name, None)
        if not changed and root not in result:
            expected.pop(root)
    else:
        changed[name] = value
    if expected != result:
        raise ConfigurationError('Native TOML structure cannot be updated in isolation', 409)
    return updated
