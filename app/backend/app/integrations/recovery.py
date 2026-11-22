"""Conservative recovery of legacy integrations, before capability migration.

Planning never writes. Application requires an unchanged complete plan and stores
private originals outside the source tree. Conflict means no application, not a
successful uninstall. No host process is started and no credential is printed.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shlex
import stat
import tomllib

from app.integrations.constants import CODEX_DAILY_MEMORY_FILES, DAILY_MEMORY_RULE_PATHS, HOST_CONFIG_PATHS, LEGACY_HOST_SKILL_PATHS, SUPPORTED_HOSTS
from app.integrations.installer import (
    _assert_path_safe, _atomic_write_file, _resolve_host_path,
    _remove_codex_toml, _clean_hook_entry, _matches_owned_entry,
)


class RecoveryConflict(ValueError):
    pass


def digest(data: bytes | None):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read_file(path: Path, home: Path):
    _assert_path_safe(path, home)
    if not path.exists():
        return None, None
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise RecoveryConflict(f'Unexpected file type or size: {path}')
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


@dataclass(frozen=True)
class Change:
    path: Path
    before: bytes | None = field(repr=False)
    after: bytes | None = field(repr=False)
    mode: int | None


CODEX_RULE_START = b"\n\n<!-- BEGIN SENRELAY WORKING MEMORY -->\n"
CODEX_RULE_END = b"<!-- END SENRELAY WORKING MEMORY -->\n"


def remove_codex_daily_rule(home: Path, manifest: dict) -> Change:
    """Return the exact owned content removal without editing user instructions."""
    paths = {_resolve_host_path(pattern, home) for pattern in CODEX_DAILY_MEMORY_FILES}
    raw_path = manifest.get('daily_rule_target')
    if not isinstance(raw_path, str) or Path(raw_path) not in paths:
        raise RecoveryConflict('Unexpected owned Codex instructions path')
    path = Path(raw_path)
    original, mode = read_file(path, home)
    if original is None:
        return Change(path, None, None, mode)
    start = original.find(CODEX_RULE_START)
    end = original.find(CODEX_RULE_END, start + len(CODEX_RULE_START))
    if (start == -1 or end == -1 or original.count(CODEX_RULE_START) != 1
            or original.count(CODEX_RULE_END) != 1):
        raise RecoveryConflict('Owned Codex working-memory block was edited or moved')
    end += len(CODEX_RULE_END)
    if digest(original[start:end]) != manifest.get('daily_rule_hash'):
        raise RecoveryConflict('Owned Codex working-memory block was edited')
    remaining = original[:start] + original[end:]
    if not remaining and manifest.get('daily_rule_created_file') is True:
        return Change(path, original, None, mode)
    return Change(path, original, remaining, mode)


def plan_codex_daily_rule(home: Path, manifest: dict, content: str) -> tuple[list[Change], dict]:
    override, regular = [_resolve_host_path(pattern, home) for pattern in CODEX_DAILY_MEMORY_FILES]
    states = {path: read_file(path, home) for path in (override, regular)}
    target = override if (states[override][0] or b'').strip() else regular
    prior = remove_codex_daily_rule(home, manifest) if manifest.get('daily_rule_hash') else None
    new_block = CODEX_RULE_START + content.encode() + CODEX_RULE_END
    changes = []
    for path, (original, mode) in states.items():
        remainder = (prior.after if prior and prior.path == path else original)
        if path == target:
            existing = remainder or b''
            if CODEX_RULE_START.strip() in existing or CODEX_RULE_END.strip() in existing:
                raise RecoveryConflict('Unowned Codex working-memory block already exists')
            remainder = existing + new_block
        changes.append(Change(path, original, remainder, mode))
    created = (manifest.get('daily_rule_created_file') is True if prior and prior.path == target
               else states[target][0] is None)
    return changes, {'daily_rule_hash': digest(new_block), 'daily_rule_target': str(target),
                     'daily_rule_created_file': created}


@dataclass(frozen=True)
class RecoveryPlan:
    host: str
    home: Path
    changes: tuple[Change, ...] = field(repr=False)

    def summary(self):
        return {'host': self.host, 'status': 'planned', 'files': [
            {'path': str(c.path), 'operation': 'delete' if c.after is None else 'remove_owned_entries',
             'before_sha256': digest(c.before), 'after_sha256': digest(c.after)}
            for c in self.changes if c.before != c.after]}


def plan_recovery(host: str, home: Path, components: list[str] | None = None) -> RecoveryPlan:
    if host not in SUPPORTED_HOSTS:
        raise RecoveryConflict('Unknown integration host')
    home = home.resolve()
    manifest_path = home/'.contextcore/manifests'/f'{host}.json'
    raw_manifest, manifest_mode = read_file(manifest_path, home)
    if raw_manifest is None:
        raise RecoveryConflict('No ownership manifest; refusing to guess')
    manifest = json.loads(raw_manifest)
    if manifest.get('agent_id') != host:
        raise RecoveryConflict('Ownership manifest host mismatch')
    installed = set(manifest.get('components', ['mcp', 'hooks', 'skill']))
    if not installed or not installed <= {'mcp', 'hooks', 'skill'}:
        raise RecoveryConflict('Invalid installed component record')
    selected = installed if components is None else set(components) if isinstance(components, list) else set()
    if not selected or not selected <= installed or (components is not None and len(selected) != len(components)):
        raise RecoveryConflict('Select distinct installed components')
    remaining = installed - selected
    changes = []
    paths = {}
    for key, config_key in [('mcp_target', 'mcp'), ('hooks_target', 'hooks'), ('skill_target', 'skill')]:
        pattern = HOST_CONFIG_PATHS[host].get(config_key) or HOST_CONFIG_PATHS[host].get('command')
        expected = _resolve_host_path(pattern, home).resolve()
        allowed = {expected}
        if key == 'skill_target':
            allowed.update(_resolve_host_path(p, home).resolve() for p in LEGACY_HOST_SKILL_PATHS.get(host, ()))
        raw_val = manifest.get(key)
        if not isinstance(raw_val, str) or not raw_val:
            raise RecoveryConflict(f'Unexpected ownership path: {key}')
        resolved_actual = _assert_path_safe(Path(raw_val), home)
        if resolved_actual not in allowed:
            raise RecoveryConflict(f'Unexpected ownership path: {key}')
        paths[key] = resolved_actual
    token = home/'.contextcore/credentials'/f'{host}.token'
    if manifest.get('token_path') != str(token):
        raise RecoveryConflict('Unexpected credential path')

    if 'mcp' in selected:
        path = paths['mcp_target']
        original, mode = read_file(path, home)
        result = original
        if original is not None:
            data = tomllib.loads(original.decode()) if host == 'codex' else json.loads(original)
            default = ['mcp_servers'] if host == 'codex' else ['mcp'] if host == 'opencode' else ['mcpServers']
            parts = manifest.get('mcp_container_path') or default
            allowed = [['mcp'], ['mcp', 'servers']] if host == 'opencode' else [default]
            if parts not in allowed:
                raise RecoveryConflict('Unexpected MCP container')
            container = data
            for part in parts:
                if not isinstance(container, dict): raise RecoveryConflict('Malformed MCP container')
                container = container.get(part, {})
            if not isinstance(container, dict): raise RecoveryConflict('Malformed MCP container')
            if 'contextcore' in container:
                if not _matches_owned_entry(manifest, container['contextcore']):
                    raise RecoveryConflict('MCP entry was externally modified')
                del container['contextcore']
                result = (_remove_codex_toml(original.decode()) if host == 'codex'
                          else json.dumps(data, indent=2, ensure_ascii=False)+'\n').encode()
        changes.append(Change(path, original, result, mode))

    if 'hooks' in selected:
        path = paths['hooks_target']
        original, mode = read_file(path, home)
        result = original
        if original is not None:
            if host in {'omp', 'opencode'}:
                if digest(original) != manifest.get('hooks_hash'):
                    raise RecoveryConflict('Extension was externally modified or has no ownership hash')
                result = None
            elif host == 'antigravity':
                # Older manifests omitted tool commands; reconstruct only the exact
                # installed template from a recorded, shell-parsed session command.
                commands = manifest.get('owned_hooks_commands', [])
                suffix = ' antigravity session-start'
                runners = [c[:-len(suffix)] for c in commands if isinstance(c, str) and c.endswith(suffix)
                           and shlex.split(c) and shlex.split(c)[-2:] == ['antigravity', 'session-start']]
                if len(set(runners)) != 1:
                    raise RecoveryConflict('Cannot prove legacy Antigravity hook ownership')
                runner = runners[0]
                expected = {
                    'contextcore-session': {'SessionStart': [{'type': 'command', 'command': runner+' antigravity session-start'}]},
                    'contextcore-invoke': {'PreInvocation': [{'type': 'command', 'command': runner+' antigravity pre-invoke'}]},
                    'contextcore-tool': {'PostToolUse': [{'matcher': '*', 'hooks': [{'command': runner+' antigravity tool'}]}]},
                    'contextcore-stop': {'Stop': [{'type': 'command', 'command': runner+' antigravity stop'}]},
                }
                data = json.loads(original)
                if not isinstance(data, dict): raise RecoveryConflict('Malformed hooks')
                for section, value in expected.items():
                    if section in data:
                        if data[section] != value:
                            raise RecoveryConflict(f'Externally modified hook: {section}')
                        del data[section]
                result = (json.dumps(data, indent=2, ensure_ascii=False)+'\n').encode()
            else:
                data = json.loads(original)
                if not isinstance(data, dict): raise RecoveryConflict('Malformed hooks')
                owned = manifest.get('owned_hooks_commands')
                if not isinstance(owned, list) or not owned or not all(isinstance(c, str) for c in owned):
                    raise RecoveryConflict('Missing hook ownership evidence')
                hooks = data.get('hooks', {})
                if not isinstance(hooks, dict): raise RecoveryConflict('Malformed hooks')
                modified = False
                for event, entries in hooks.items():
                    if not isinstance(entries, list): raise RecoveryConflict('Malformed hook entries')
                    cleaned = []
                    for entry in entries:
                        value, changed = _clean_hook_entry(entry, set(owned))
                        modified |= changed
                        if value is not None: cleaned.append(value)
                    hooks[event] = cleaned
                if modified:
                    result = (json.dumps(data, indent=2, ensure_ascii=False)+'\n').encode()
        changes.append(Change(path, original, result, mode))

    owned_files = []
    if 'skill' in selected:
        owned_files.append((paths['skill_target'], 'skill_hash', False))
        if host in DAILY_MEMORY_RULE_PATHS and manifest.get('daily_rule_hash'):
            owned_files.append((_resolve_host_path(DAILY_MEMORY_RULE_PATHS[host], home), 'daily_rule_hash', False))
        if host == 'codex' and manifest.get('daily_rule_hash'):
            changes.append(remove_codex_daily_rule(home, manifest))
    if installed & {'mcp', 'hooks'} and not remaining & {'mcp', 'hooks'}:
        owned_files.append((token, 'token_hash', True))
    for path, hash_key, stripped in owned_files:
        original, mode = read_file(path, home)
        if original is not None and digest(original.strip() if stripped else original) != manifest.get(hash_key):
            raise RecoveryConflict(f'Owned file changed or lacks proof: {path}')
        changes.append(Change(path, original, None, mode))
    after_manifest = None
    if remaining:
        manifest['components'] = sorted(remaining)
        for component, fields in {'mcp': ['owned_mcp_entry', 'owned_mcp_entry_sha256', 'mcp_container_path', 'mcp_shape'],
                                  'hooks': ['owned_hooks_commands', 'hooks_hash'],
                                  'skill': ['skill_hash', 'daily_rule_hash', 'daily_rule_target', 'daily_rule_created_file']}.items():
            if component in selected:
                for field_name in fields: manifest.pop(field_name, None)
        if not remaining & {'mcp', 'hooks'}: manifest.pop('token_hash', None)
        # A partial uninstall also removes legacy plaintext from retained ownership metadata.
        if 'owned_mcp_entry' in manifest:
            from app.integrations.installer import _entry_digest
            manifest['owned_mcp_entry_sha256'] = _entry_digest(manifest.pop('owned_mcp_entry'))
        after_manifest = (json.dumps(manifest, indent=2, ensure_ascii=False)+'\n').encode()
    changes.append(Change(manifest_path, raw_manifest, after_manifest, manifest_mode))
    return RecoveryPlan(host, home, tuple(changes))


def apply_recovery(plan: RecoveryPlan, backup: Path, *, confirmed=False):
    if confirmed is not True: raise RecoveryConflict('Explicit recovery confirmation required')
    for change in plan.changes:
        if read_file(change.path, plan.home) != (change.before, change.mode):
            raise RecoveryConflict(f'Configuration changed after preview: {change.path}')
    _assert_path_safe(backup, plan.home)
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    backup.chmod(0o700)
    receipt = {**plan.summary(), 'status': 'applying', 'applied': []}
    def save():
        _atomic_write_file(backup/'receipt.json', json.dumps(receipt, indent=2)+'\n', mode=0o600)
    for index, change in enumerate(plan.changes):
        if change.before is not None:
            _atomic_write_file(backup/f'{index}.original', change.before, mode=0o600)
    receipt['restore_index'] = [{'path': str(c.path), 'original': f'{i}.original' if c.before is not None else None,
                                 'mode': c.mode} for i, c in enumerate(plan.changes)]
    save()
    applied = []
    try:
        for change in plan.changes:
            if read_file(change.path, plan.home) != (change.before, change.mode):
                raise RecoveryConflict(f'Configuration changed during recovery: {change.path}')
            if change.before == change.after: continue
            if change.after is None:
                change.path.unlink()
                if change.path.parent.name in ('contextcore', 'contextcore-handoff'):
                    try:
                        change.path.parent.rmdir()
                    except OSError:
                        pass
            else: _atomic_write_file(change.path, change.after, mode=change.mode)
            applied.append(change)
            if read_file(change.path, plan.home)[0] != change.after:
                raise RecoveryConflict(f'Recovery readback failed: {change.path}')
            receipt['applied'].append(str(change.path)); save()
        receipt['status'] = 'detached'; save()
        return receipt
    except BaseException:
        receipt['status'] = 'rolled_back'
        for change in reversed(applied):
            try:
                if read_file(change.path, plan.home)[0] != change.after:
                    raise RecoveryConflict('Concurrent edit during rollback')
                if change.before is None: change.path.unlink(missing_ok=True)
                else: _atomic_write_file(change.path, change.before, mode=change.mode)
            except Exception:
                receipt['status'] = 'recovery_required'
        save()
        raise
