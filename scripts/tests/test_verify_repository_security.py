"""Behavioral counterexamples for the release security gates (stdlib only)."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "repository_security", Path(__file__).resolve().parents[1] / "verify_repository_security.py"
)
security = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(security)


def completed(body, status=0):
    return subprocess.CompletedProcess([], status, json.dumps(body), "")


def clean_npm():
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {},
        "metadata": {
            "vulnerabilities": dict(info=0, low=0, moderate=0, high=0, critical=0, total=0),
            "dependencies": dict(prod=1, dev=0, optional=0, peer=0, peerOptional=0, total=1),
        },
    }


def clean_python():
    return {"dependencies": [{"name": "example", "version": "1.0", "vulns": []}], "fixes": []}


class ScannerStatusTests(unittest.TestCase):
    def frontend(self, result):
        with patch.object(security, "run", return_value=result), redirect_stdout(io.StringIO()):
            return security.check_frontend_audit()

    def python(self, result):
        exported = subprocess.CompletedProcess([], 0, "example==1.0\n", "")
        with patch.object(security, "run", side_effect=[exported, result]), redirect_stdout(io.StringIO()):
            return security.check_python_security()

    def test_npm_registry_error_is_not_a_zero_advisory_report(self):
        self.assertFalse(self.frontend(completed({"error": {"summary": "registry unavailable"}}, 1)))

    def test_only_complete_successful_zero_npm_report_passes(self):
        self.assertTrue(self.frontend(completed(clean_npm())))
        self.assertFalse(self.frontend(completed(clean_npm(), 1)))
        self.assertFalse(self.frontend(completed({})))
        report = clean_npm()
        report["metadata"]["vulnerabilities"]["total"] = "0"
        self.assertFalse(self.frontend(completed(report)))
        report = clean_npm()
        report["metadata"]["dependencies"] = {}
        self.assertFalse(self.frontend(completed(report)))

    def test_npm_advisories_block_even_with_zero_exit(self):
        report = clean_npm()
        report["metadata"]["vulnerabilities"].update(low=1, total=1)
        report["vulnerabilities"]["example"] = {"severity": "low"}
        self.assertFalse(self.frontend(completed(report)))

    def test_python_advisories_block_regardless_of_exit_status(self):
        report = clean_python()
        report["dependencies"][0]["vulns"] = [{"id": "TEST-ADVISORY", "fix_versions": []}]
        for status in (0, 1):
            with self.subTest(status=status):
                self.assertFalse(self.python(completed(report, status)))

    def test_python_incomplete_skipped_or_failed_reports_block(self):
        skipped = {"dependencies": [{"name": "example", "skip_reason": "feed unavailable"}], "fixes": []}
        for result in (
            completed(clean_python(), 1), completed({}), completed({"dependencies": [], "fixes": []}),
            completed(skipped), completed({"dependencies": clean_python()["dependencies"]}),
            subprocess.CompletedProcess([], 0, "not JSON", "private diagnostic"),
        ):
            with self.subTest(result=result):
                self.assertFalse(self.python(result))
        self.assertTrue(self.python(completed(clean_python())))

    def test_python_report_must_cover_exact_graph_without_duplicates(self):
        report = clean_python()
        self.assertFalse(security.python_report_is_clean(report, 0, {("example", "2.0")}))
        self.assertFalse(security.python_report_is_clean(report, 0, {("example", "1.0"), ("other", "1.0")}))
        report["dependencies"].append(dict(report["dependencies"][0]))
        self.assertFalse(security.python_report_is_clean(report, 0, {("example", "1.0")}))

    def test_unavailable_scanners_fail_without_echoing_diagnostics(self):
        private = "sensitive diagnostic must not appear"
        for check in (security.check_python_security, security.check_frontend_audit):
            output = io.StringIO()
            with patch.object(security, "run", side_effect=FileNotFoundError(private)), redirect_stdout(output):
                self.assertFalse(check())
            self.assertNotIn(private, output.getvalue())

    def test_failed_export_does_not_run_an_alternate_audit(self):
        with patch.object(security, "run", return_value=subprocess.CompletedProcess([], 1, "", "")) as run:
            with redirect_stdout(io.StringIO()):
                self.assertFalse(security.check_python_security())
        self.assertEqual(run.call_count, 1)

    def test_runtime_graph_broadens_markers_but_rejects_nonpins(self):
        self.assertEqual(security.runtime_pins("thing_name==1.2 ; sys_platform == 'win32'\n"), {("thing-name", "1.2")})
        for export in ("", "thing>=1.2", "thing @ https://example.invalid/pkg.whl", "-r other.txt"):
            with self.subTest(export=export), self.assertRaises(ValueError):
                security.runtime_pins(export)

    def test_nonzero_result_propagates_to_entrypoint(self):
        with patch.object(security, "check_frontend_audit", return_value=True), \
             patch.object(security, "check_python_security", return_value=False), \
             patch.object(security, "check_repository_hygiene", return_value=True), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(security.main(), 1)

    def test_env_examples_are_allowed_artifacts_but_still_scanned(self):
        self.assertFalse(security.prohibited_path(".env.example"))
        self.assertFalse(security.prohibited_path("app/backend/.env.example"))
        self.assertTrue(security.prohibited_path("app/backend/.env"))
        credential = "ghp_" + "D" * 36
        self.assertTrue(security.content_findings(credential.encode(), "app/backend/.env.example"))

    def test_relative_locations_redact_tokens_and_escape_control_characters(self):
        credential = "ghp_" + "E" * 36
        location = security.safe_relative_location("tests/\n" + credential)
        self.assertNotIn(credential, location)
        self.assertNotIn(credential[:8], location)
        self.assertNotIn("\n", location)
        self.assertIn("[REDACTED]", location)

    def test_reviewed_fixture_exception_is_exact_path_and_match_only(self):
        path = "app/backend/tests/test_provider_checks.py"
        assignment = b'api_key": "' + b"never-return-or-persist-secret" + b'"'
        self.assertFalse(security.content_findings(assignment, path))
        self.assertTrue(security.content_findings(assignment, "app/backend/tests/another.py"))
        changed = assignment.replace(b"persist-secret", b"persist-secret-changed")
        self.assertTrue(security.content_findings(changed, path))
        additional = assignment + b"\n" + b"ghp_" + b"F" * 36
        self.assertTrue(security.content_findings(additional, path))
        additional_assignment = assignment + b"\n" + b'api_key": "' + b"G" * 36 + b'"'
        self.assertTrue(security.content_findings(additional_assignment, path))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(security.inspect_source(path, assignment, "reviewed fixture"))
            self.assertFalse(security.inspect_source(path, additional, "reviewed fixture"))
        self.assertNotIn("never-return-or-persist-secret", output.getvalue())
        self.assertNotIn("ghp_" + "F" * 8, output.getvalue())


class HygieneBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cc-security-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        (self.root / "source.py").write_text("print('safe')\n")
        self.commit()
        self.root_patch = patch.object(security, "REPO_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=Security Fixture", "-c", "user.email=fixture@example.invalid",
                               "-c", "commit.gpgsign=false", *args], cwd=self.root, capture_output=True, check=True)

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-q", "-m", "synthetic scanner fixture")

    def scan(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = security.check_repository_hygiene()
        return result, output.getvalue()

    def test_clean_source_and_history_pass_bounded_scan(self):
        result, output = self.scan()
        self.assertTrue(result, output)

    def test_untracked_tests_and_qa_are_not_exempt(self):
        credential = "ghp_" + "A" * 36
        for directory in ("tests", "qa"):
            with self.subTest(directory=directory):
                target = self.root / directory / "fixture.txt"
                target.parent.mkdir()
                target.write_text(credential)
                result, output = self.scan()
                self.assertFalse(result)
                self.assertNotIn(credential, output)
                self.assertNotIn(credential[:8], output)
                target.unlink()

    def test_deleted_credentials_remain_detectable_in_history(self):
        credential = "ghp_" + "B" * 36
        target = self.root / "removed.txt"
        target.write_text(credential)
        self.commit()
        target.unlink()
        self.commit()
        result, output = self.scan()
        self.assertFalse(result)
        self.assertIn("Git blob", output)
        self.assertNotIn(credential, output)
        self.assertNotIn(credential[:8], output)

    def test_generated_directories_only_are_excluded(self):
        generated = self.root / "node_modules" / "fixture.txt"
        generated.parent.mkdir()
        generated.write_text("ghp_" + "C" * 36)
        self.assertTrue(self.scan()[0])
        (self.root / "tests").mkdir()
        (self.root / "tests" / "service.db").write_bytes(b"runtime data")
        self.assertFalse(self.scan()[0])

    def test_unreadable_inventory_and_shallow_history_are_unknown(self):
        for response in (ValueError("private"), b"true\n"):
            with self.subTest(response=response), patch.object(security, "git", side_effect=response if isinstance(response, Exception) else None,
                                                               return_value=response):
                self.assertFalse(self.scan()[0])

    def test_shallow_history_reports_the_actionable_prerequisite(self):
        with patch.object(security, "git", return_value=b"true\n"):
            result, output = self.scan()
        self.assertFalse(result)
        self.assertIn("shallow-history", output)
        self.assertIn("fetch-depth: 0", output)

    def test_oversized_source_reports_relative_path_without_content(self):
        target = self.root / "oversized.txt"
        target.write_bytes(b"sensitive-content" * 100)
        with patch.object(security, "MAX_SOURCE_BYTES", 100):
            result, output = self.scan()
        self.assertFalse(result)
        self.assertIn("oversized-source", output)
        self.assertIn("oversized.txt", output)
        self.assertNotIn("sensitive-content", output)

    def test_oversized_history_reports_blob_and_path_without_content(self):
        target = self.root / "removed-large.txt"
        target.write_bytes(b"sensitive-history" * 100)
        self.commit()
        target.unlink()
        self.commit()
        with patch.object(security, "MAX_SOURCE_BYTES", 100):
            result, output = self.scan()
        self.assertFalse(result)
        self.assertIn("oversized-history-blob", output)
        self.assertIn("Git blob", output)
        self.assertIn("removed-large.txt", output)
        self.assertNotIn("sensitive-history", output)

    def test_symlink_is_not_followed_outside_source_root(self):
        (self.root / "link").symlink_to(Path(self.temporary.name).parent)
        self.assertFalse(self.scan()[0])

    def test_symlinked_source_ancestor_is_rejected_before_reading(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "source.txt").write_text("safe")
        self.commit()
        (nested / "source.txt").unlink()
        nested.rmdir()
        nested.symlink_to(self.root.parent)
        # The cached nested/source.txt entry still exists in Git's inventory.
        self.assertTrue(security.source_has_symlink("nested/source.txt"))
        self.assertFalse(self.scan()[0])


if __name__ == "__main__":
    unittest.main()
