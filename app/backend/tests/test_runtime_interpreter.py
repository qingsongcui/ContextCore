import json
import plistlib
import subprocess
import venv

from app import runtime


def test_launchagent_python_preserves_virtualenv_runtime(tmp_path):
    environment = tmp_path / 'service-env'
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    interpreter = environment / 'bin' / 'python'
    plist = plistlib.loads(runtime.generate_launchd_plist(
        data_dir=tmp_path / 'data', python_path=interpreter,
    ).encode())
    actual = json.loads(subprocess.check_output([
        plist['ProgramArguments'][0], '-c',
        'import json,sys; print(json.dumps({"prefix":sys.prefix,"base_prefix":sys.base_prefix}))',
    ], text=True))
    assert actual['prefix'] == str(environment)
    assert actual['prefix'] != actual['base_prefix']
