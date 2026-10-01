"""Wheel-installed workflow in a foreign cwd, without another Kaiten CLI (not live E2E)."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


@pytest.fixture(scope="module")
def installed_kaiten_surface(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("kaiten-wheel")
    repository = Path(__file__).resolve().parents[2]
    source = workspace / "source"
    source.mkdir()
    for name in ("src",):
        shutil.copytree(repository / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(repository / name, source / name)
    uv = shutil.which("uv")
    assert uv, "Missing dependency: uv is required; no global installation is attempted."
    environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "CLI_ALL_ROOT"}}
    wheels, venv = workspace / "wheels", workspace / "venv"
    for args in ([uv, "build", "--wheel", "--out-dir", str(wheels), str(source)],
                 [uv, "venv", "--python", sys.executable, str(venv)]):
        result = subprocess.run(args, cwd=workspace, env=environment, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
    wheel = next(wheels.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert 'kaiten_cli/cli.py' in names and 'kaiten_cli/SKILL.md' in names
        assert not any(name.startswith(('cli_all/', 'services/')) or '/templates/' in name for name in names)
        metadata = archive.read(next(name for name in names if name.endswith('/METADATA'))).decode()
        assert 'Requires-Dist: click' in metadata
        assert 'Requires-Dist: cli-all' not in metadata
    result = subprocess.run([uv, "pip", "install", "--python", str(venv / "bin/python"), str(wheel)],
                            cwd=workspace, env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return venv / "bin/kaiten"


def test_installed_native_setup_without_other_cli(installed_kaiten_surface, tmp_path):
    environment = {k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME', 'CLI_ALL_ROOT'} and not k.startswith('KAITEN_')}
    environment.update(PATH=str(tmp_path / 'empty-bin'), XDG_CONFIG_HOME=str(tmp_path / 'personal'))

    def invoke(*args, input=None):
        result = subprocess.run([str(installed_kaiten_surface), '--json', *args], cwd=tmp_path,
            env=environment, input=input, capture_output=True, text=True, timeout=10)
        return result.returncode, json.loads(result.stdout)
    code, skill = invoke('skill', 'install', '--directory', str(tmp_path / 'agent-skills'))
    assert code == 0 and skill['data']['status'] == 'installed'
    assert Path(skill['data']['path']).read_text().startswith('---\nname: kaiten-cli\n')
    code, instructions = invoke('setup')
    assert code == 0 and 'token' in ' '.join(instructions['data']['instructions']).lower()
    code, doctor = invoke('doctor')
    assert code == 1 and doctor['error']['details']['token_available'] is False
    code, saved = invoke('setup', '--profile', 'test-profile', '--tenant', 'example.kaiten.ru', '--board', '12', '--confirm')
    assert code == 0 and saved['data']['saved']
    code, auth = invoke('auth', 'login', '--profile', 'test-profile', '--tenant', 'example.kaiten.ru', '--token-stdin', input='synthetic-wheel-secret\n')
    assert code == 0 and 'synthetic-wheel-secret' not in json.dumps(auth)
    code, doctor = invoke('doctor')
    assert code == 0 and doctor['data']['healthy'] and doctor['data']['backend'] == 'direct_api'
    code, denied = invoke('cards', 'view', '33', '--board', '99')
    assert code == 1 and denied['error']['code'] == 'forbidden'
    code, failure = invoke('setup', '--token', 'synthetic-secret-do-not-echo')
    assert code == 2 and failure['error']['code'] == 'cli_usage_error'
    assert 'synthetic-secret' not in json.dumps(failure)
