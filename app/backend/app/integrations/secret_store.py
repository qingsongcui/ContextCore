"""Credential references only. No plaintext fallback when Keychain is unavailable."""
from __future__ import annotations

import json
import hmac
import subprocess
import sys
from typing import Protocol
from uuid import uuid4


class SecretUnavailable(RuntimeError):
    def __init__(self, message: str, reference: str | None = None):
        super().__init__(message)
        self.reference = reference


class SecretStore(Protocol):
    def put(self, value: str) -> str: ...
    def get(self, reference: str) -> str: ...
    def delete(self, reference: str) -> None: ...


class KeychainSecretStore:
    service = "net.senrelay.management"

    def _run(self, args: list[str], input_text: str | None = None) -> str:
        if sys.platform != "darwin":
            raise SecretUnavailable("macOS Keychain is unavailable; credentials were not stored")
        try:
            result = subprocess.run(["/usr/bin/security", *args], input=input_text,
                                    text=True, capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise SecretUnavailable("Keychain is unavailable or locked") from None
        if result.returncode:
            raise SecretUnavailable("Keychain denied access or the credential is unavailable")
        return result.stdout.rstrip("\n")

    def put(self, value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 16384 or any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("Credential must be a non-empty string of at most 16384 characters")
        reference = f"keychain:senrelay:{uuid4().hex}"
        # security's interactive command interpreter accepts -w without putting
        # the secret in process argv. JSON quotes escape newlines and quotes.
        command = "add-generic-password -U -a {} -s {} -w {}\n".format(
            json.dumps(reference), json.dumps(self.service), json.dumps(value))
        try:
            self._run(["-i"], command)
        except SecretUnavailable:
            raise
        try:
            # security's interactive interpreter may not communicate an inner
            # command failure via its exit code. Verify the stored value before
            # exposing a configured credential reference.
            stored = self.get(reference)
            if not hmac.compare_digest(stored.encode(), value.encode()):
                raise SecretUnavailable("Keychain credential readback did not match")
        except SecretUnavailable:
            raise SecretUnavailable("Keychain credential could not be verified", reference=reference) from None
        return reference

    def get(self, reference: str) -> str:
        self._validate(reference)
        return self._run(["find-generic-password", "-a", reference, "-s", self.service, "-w"])

    def delete(self, reference: str) -> None:
        self._validate(reference)
        self._run(["delete-generic-password", "-a", reference, "-s", self.service])

    @staticmethod
    def _validate(reference: str) -> None:
        if not reference.startswith("keychain:senrelay:") or len(reference) != 50:
            raise ValueError("Invalid Senrelay credential reference")
