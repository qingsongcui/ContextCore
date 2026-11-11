"""Explicit public SemVer -> canonical Python and native version mapping."""
from __future__ import annotations

import re


def release_versions(version: str) -> dict[str, str]:
    match = re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(alpha|beta|rc)\.([1-9][0-9]*))?', version)
    if not match:
        raise ValueError('Expected MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-{alpha,beta,rc}.N')
    base = '.'.join(match.group(1, 2, 3))
    stage, number = match.group(4, 5)
    suffix = {'alpha': 'a', 'beta': 'b', 'rc': 'rc'}[stage] + number if stage else ''
    return {'version': version, 'tag': 'v' + version, 'python_version': base + suffix, 'native_version': base}


BUILD_NUMBER_PATTERN = re.compile(r'[0-9]{1,7}(?:\.[0-9]{1,2}){0,2}')


def normalize_ci_build_number(run_number: str) -> str:
    value = run_number.strip()
    if value == '999' or value.startswith('999.'):
        return '998.1'
    return value


def validate_build_number(build_number: str) -> str:
    if not BUILD_NUMBER_PATTERN.fullmatch(build_number):
        raise ValueError('Build number must contain up to three numeric components')
    reject_audit_build_number(build_number)
    return build_number


def reject_audit_build_number(build_number: str) -> None:
    if build_number == '999' or build_number.startswith('999.'):
        raise ValueError('Build 999 is an audit temporary number and cannot be a release or daily update target')


def validate_versions(*, frontend: str, backend: str, tag: str | None = None, wheel: str | None = None) -> dict[str, str]:
    mapped = release_versions(frontend)
    if backend != mapped['python_version']:
        raise ValueError('Backend version does not match canonical Python release version')
    if tag is not None and tag != mapped['tag']:
        raise ValueError('Tag does not match frontend release version')
    if wheel is not None and wheel != mapped['python_version']:
        raise ValueError('Wheel metadata does not match canonical Python release version')
    return mapped
