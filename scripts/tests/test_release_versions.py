import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('release_versions', Path(__file__).resolve().parents[2] / 'app/scripts/release_versions.py')
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)


class ReleaseVersionsTest(unittest.TestCase):
    def test_supported_release_identity(self):
        for public, python in [('1.0.0', '1.0.0'), ('1.0.0-beta.1', '1.0.0b1'), ('2.3.4-rc.2', '2.3.4rc2'), ('2.0.0-alpha.1', '2.0.0a1')]:
            with self.subTest(public=public):
                mapped = versions.validate_versions(frontend=public, backend=python, tag='v'+public, wheel=python)
                self.assertEqual(mapped['native_version'], public.split('-')[0])

    def test_mismatch_or_unknown_version_is_blocked(self):
        for options in [
            dict(frontend='1.0.0-beta.1', backend='1.0.0-beta.1'),
            dict(frontend='1.0.0-beta.1', backend='1.0.0b1', tag='v1.0.0'),
            dict(frontend='1.0.0-beta.1', backend='1.0.0b1', wheel='1.0.0b2'),
            dict(frontend='01.0.0', backend='1.0.0'),
            dict(frontend='1.0.0-beta.0', backend='1.0.0b0'),
            dict(frontend='1.0.0-dev.1', backend='1.0.0.dev1'),
            dict(frontend='1.0.0+unknown', backend='1.0.0'),
        ]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                versions.validate_versions(**options)

    def test_audit_build_999_is_not_a_release_target(self):
        for number in ('999', '999.1', '999.1.2'):
            with self.subTest(number=number), self.assertRaises(ValueError):
                versions.reject_audit_build_number(number)
        versions.reject_audit_build_number('1000')
