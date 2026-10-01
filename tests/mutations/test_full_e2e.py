"""Wheel-installed CLI plus our direct HTTP backend with synthetic responses, never production API."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture(scope="module")
def installed_surface(tmp_path_factory):
    work = tmp_path_factory.mktemp("kaiten-mutation-wheel")
    repo = Path(__file__).resolve().parents[2]
    source = work / "source"
    source.mkdir()
    for name in ("src",):
        shutil.copytree(repo / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(repo / name, source / name)
    uv = shutil.which("uv")
    assert uv
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME", "CLI_ALL_ROOT"}}
    wheels, venv = work / "wheels", work / "venv"
    for command in ([uv, "build", "--wheel", "--out-dir", str(wheels), str(source)],
                    [uv, "venv", "--python", sys.executable, str(venv)]):
        reply = subprocess.run(command, cwd=work, env=environment, capture_output=True, text=True, timeout=60)
        assert reply.returncode == 0, reply.stderr
    reply = subprocess.run([uv, "pip", "install", "--python", str(venv / "bin/python"), str(next(wheels.glob("*.whl")))],
                           cwd=work, env=environment, capture_output=True, text=True, timeout=60)
    assert reply.returncode == 0, reply.stderr
    return venv / "bin"


def test_installed_comment_and_move_workflow(installed_surface, tmp_path, monkeypatch):
    from test_backend_contract import local_api
    driver = tmp_path / 'native_driver.py'
    driver.write_text(r'''import runpy, sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from kaiten_cli.client import KaitenClient, NoRedirect
origin, binary = sys.argv[1:3]
original_init = KaitenClient.__init__
opener = build_opener(NoRedirect())
def local_transport(request, **kwargs):
    parsed = urlsplit(request.full_url)
    local = Request(origin + parsed.path + ('?' + parsed.query if parsed.query else ''),
                    data=request.data, method=request.method, headers=dict(request.headers))
    return opener.open(local, **kwargs)
def fixture_init(self, credentials, **kwargs):
    original_init(self, credentials, open_request=local_transport)
KaitenClient.__init__ = fixture_init
sys.argv = [binary, *sys.argv[3:]]
runpy.run_path(binary, run_name='__main__')
''', encoding='utf-8')
    env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME', 'CLI_ALL_ROOT'} and not key.startswith('KAITEN_')}
    env['XDG_CONFIG_HOME'] = str(tmp_path / 'personal')
    # A trap verifies that our installed CLI never invokes a separate kaiten binary.
    trap = tmp_path / 'kaiten'
    trap.write_text('#!/bin/sh\nexit 99\n')
    trap.chmod(0o700)
    env['PATH'] = str(tmp_path)
    monkeypatch.chdir(tmp_path)
    with local_api() as (origin, calls):
        def invoke(*args, code=0, input=None):
            reply = subprocess.run([str(installed_surface / 'python'), str(driver), origin,
                str(installed_surface / 'kaiten'), '--json', *args],
                env=env, input=input, capture_output=True, text=True, timeout=10)
            assert reply.returncode == code, reply.stdout + reply.stderr
            return json.loads(reply.stdout)
        invoke('auth', 'login', '--profile', 'mpstats', '--tenant', 'mpstats.kaiten.ru', '--token-stdin', input='synthetic-installed\n')
        discovered = invoke('setup-discovery', '--profile', 'mpstats', '--tenant', 'mpstats.kaiten.ru', '--space', '2')
        assert discovered['data']['boards'] == [{'id': 12, 'title': 'Work', 'space_id': 2}]
        invoke('setup', '--profile', 'mpstats', '--tenant', 'mpstats.kaiten.ru', '--board', '12', '--confirm')
        assert invoke('doctor', '--online')['data']['healthy']
        question = invoke('comments', 'prepare', '33', '--text', 'Готово')
        assert question['data']['ready'] is False
        preview = invoke('comments', 'prepare', '33', '--text', 'Готово', '--mention', 'нет')['data']
        invoke('comments', 'post', '33', '--text', 'Изменено', '--mention', 'нет', '--approval', preview['approval'], code=1)
        dry_run = invoke('comments', 'post', '33', '--text', 'Готово', '--mention', 'нет', '--approval', preview['approval'], '--dry-run')
        assert dry_run['data']['dry_run']
        sent = invoke('comments', 'post', '33', '--text', 'Готово', '--mention', 'нет', '--approval', preview['approval'])
        assert sent['data']['status'] == 'applied' and sent['data']['comment']['text'] == 'Готово'
        assert invoke('comments', 'list', '33')['data']['comments'][0]['internal'] is True
        move = invoke('move', 'prepare', '33', '--column', '6')['data']
        moved = invoke('move', 'apply', '33', '--column', '6', '--authorization', 'перемещай', '--approval', move['approval'])
        assert moved['data']['location'] == {'board_id': 12, 'column_id': 6, 'lane_id': 5}
    assert sum(method == 'POST' for method, _, _, _ in calls) == 1
    assert sum(method == 'PATCH' for method, _, _, _ in calls) == 1
    assert all(auth == 'Bearer synthetic-installed' for _, _, _, auth in calls)


def test_packaged_skill_matches_canonical():
    repo = Path(__file__).resolve().parents[2]
    assert (repo / "src/kaiten_cli/SKILL.md").read_bytes() == (repo / "skills/kaiten-cli/SKILL.md").read_bytes()
