"""A stopped registered job is distinct from an unreadable job identity."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import runtime


@pytest.mark.parametrize("output,returncode,expected", [
    ("job = {\n state = running\n pid = 55555\n}\n", 0, 55555),
    ("job = {\n state = not running\n}\n", 0, 0),
    ("job = {\n state = waiting\n}\n", 0, None),
    ("job = {\n state = running\n}\n", 0, None),
    ("job = {\n pid = 55555\n pid = 66666\n}\n", 0, None),
    ("state = not running\n", 1, None),
])
def test_launchd_identity_distinguishes_absent_process_from_unknown(output, returncode, expected):
    with patch("app.runtime.subprocess.run", return_value=SimpleNamespace(
        stdout=output, returncode=returncode,
    )):
        assert runtime._registered_launchd_pid("com.contextcore.synthetic") == expected


def test_launchd_query_failure_does_not_authorize_direct_stop():
    with patch("app.runtime.subprocess.run", side_effect=OSError("unavailable")):
        assert runtime._registered_launchd_pid("com.contextcore.synthetic") is None
