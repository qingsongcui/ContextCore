"""Keychain adapter tests never call the live security(1) binary."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app.integrations.secret_store import KeychainSecretStore, SecretUnavailable


REFERENCE = "keychain:senrelay:" + "a" * 32


def test_non_darwin_and_denied_access_fail_closed_without_a_usable_reference(monkeypatch):
    store = KeychainSecretStore()
    monkeypatch.setattr("app.integrations.secret_store.sys.platform", "linux")
    with pytest.raises(SecretUnavailable, match="unavailable"):
        store.put("synthetic-only-value")
    monkeypatch.setattr("app.integrations.secret_store.sys.platform", "darwin")

    def denied(args, **kwargs):
        return subprocess.CompletedProcess(args, 51, stdout="", stderr="locked")

    monkeypatch.setattr(subprocess, "run", denied)
    with pytest.raises(SecretUnavailable) as missing:
        store.get(REFERENCE)
    assert missing.value.reference is None
    with pytest.raises(SecretUnavailable):
        store.delete(REFERENCE)
    with pytest.raises(ValueError, match="Invalid"):
        store.get("env:OTHER_KEY")


def test_lock_timeout_and_missing_item_do_not_fall_back_to_plaintext(monkeypatch):
    store = KeychainSecretStore()
    monkeypatch.setattr("app.integrations.secret_store.sys.platform", "darwin")

    def locked(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 15)

    monkeypatch.setattr(subprocess, "run", locked)
    with pytest.raises(SecretUnavailable, match="locked"):
        store.get(REFERENCE)

    def missing(args, **kwargs):
        return subprocess.CompletedProcess(args, 44, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(SecretUnavailable):
        store.get(REFERENCE)


def test_unverified_put_exposes_the_reference_for_cleanup_and_does_not_return_it(monkeypatch):
    store = KeychainSecretStore()
    monkeypatch.setattr("app.integrations.secret_store.sys.platform", "darwin")
    monkeypatch.setattr("app.integrations.secret_store.uuid4", lambda: SimpleNamespace(hex="b" * 32))
    calls = []

    def handler(args, **kwargs):
        calls.append(args)
        if "-i" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="other-stored-value", stderr="")

    monkeypatch.setattr(subprocess, "run", handler)
    with pytest.raises(SecretUnavailable) as failed:
        store.put("synthetic-only-value")
    assert failed.value.reference == "keychain:senrelay:" + "b" * 32
    assert any("-i" in call for call in calls)
    assert any("find-generic-password" in call for call in calls)
