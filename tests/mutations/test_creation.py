"""Card creation over real HTTP with synthetic data, including installed wheels."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import threading
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

import pytest

from kaiten_cli.auth import CredentialStore
from kaiten_cli.client import KaitenClient, NoRedirect
from kaiten_cli.creation import KaitenCreation
from kaiten_cli.policy import KaitenError, LocalConfigStore
from test_full_e2e import installed_surface


@contextmanager
def creation_api():
    state = dict(calls=[], cards={}, reply={}, readback={}, status=200, readback_status=200,
                 multi_lane=False, missing_id=False, no_responsible=False,
                 users=[dict(id=7, full_name='Иван Иванов', username='ivan', activated=True, email='PRIVATE')])

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

        def do_GET(self):
            path = urlsplit(self.path).path.removeprefix('/api/latest')
            state['calls'].append(('GET', path, None))
            if path == '/boards/12/columns':
                return self.respond(200, [dict(id=4, title='Очередь', board_id=12)])
            if path == '/boards/12/lanes':
                lanes = [dict(id=5, title='Основная', board_id=12)]
                if state['multi_lane']:
                    lanes.append(dict(id=6, title='Вторая', board_id=12))
                return self.respond(200, lanes)
            if path == '/users':
                return self.respond(200, state['users'])
            if path.endswith('/members'):
                card_id = int(path.split('/')[2]); card = state['cards'][card_id]
                members = [] if state['no_responsible'] else [dict(card_id=card_id, user_id=card.get('responsible_id'), type=2)]
                return self.respond(200, members)
            if path.startswith('/cards/'):
                card = state['cards'].get(int(path.split('/')[2]), {})
                return self.respond(state['readback_status'], {**card, **state['readback'], 'owner': {'email': 'PRIVATE'}})
            return self.respond(404, {})

        def do_POST(self):
            path = urlsplit(self.path).path.removeprefix('/api/latest')
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(('POST', path, body))
            assert path == '/cards' and self.headers['Authorization'] == 'Bearer synthetic-create'
            if state['status'] != 200:
                return self.respond(state['status'], {'error': 'PRIVATE'})
            card_id = len(state['cards']) + 100
            card = dict(id=card_id, state=1, **body)
            if 'due_date' in body:
                card['due_date'] += 'T00:00:00.000Z'
            state['cards'][card_id] = card
            reply = {**card, **state['reply'], 'url': 'PRIVATE'}
            if state['missing_id']:
                reply.pop('id')
            self.respond(200, reply)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown(); thread.join(timeout=3); server.server_close()


@pytest.fixture
def creation(tmp_path, monkeypatch):
    monkeypatch.delenv('KAITEN_TOKEN', raising=False)
    store = LocalConfigStore(tmp_path / 'personal/policy.json')
    store.save(profile='work', tenant='example.kaiten.ru', board_allowlist=[12])
    credentials = CredentialStore(tmp_path / 'personal/profiles.json')
    credentials.save_credentials('work', 'example.kaiten.ru', 'synthetic-create')
    with creation_api() as (origin, state):
        opener = build_opener(NoRedirect())
        def transport(request, **kwargs):
            parsed = urlsplit(request.full_url)
            return opener.open(Request(origin + parsed.path + ('?' + parsed.query if parsed.query else ''),
                data=request.data, method=request.method, headers=dict(request.headers)), **kwargs)
        yield KaitenCreation(store, credentials=credentials,
                            client_factory=lambda selected: KaitenClient(selected, open_request=transport)), state


def create(adapter, **kwargs):
    options = dict(board_id=12, column_id=4, authorization='direct', **kwargs)
    return adapter.create_card('Проверить выгрузку', **options)


def test_exact_card_and_responsible_readback(creation):
    adapter, state = creation
    result = create(adapter, description='Проверить\nотчёт', due_date='2026-10-15', responsible='@ivan')
    assert result['status'] == 'applied' and result['readback']
    assert result['url'] == 'https://example.kaiten.ru/boards/card/100'
    writes = [c for c in state['calls'] if c[0] == 'POST']
    assert writes == [('POST', '/cards', dict(title='Проверить выгрузку', board_id=12, column_id=4,
        lane_id=5, description='Проверить\nотчёт', due_date='2026-10-15', due_date_time_present=False, responsible_id=7))]
    assert 'PRIVATE' not in json.dumps(result) and result['retry_safe'] is False


@pytest.mark.parametrize('options,code', [
    ({'title': ''}, 'title_invalid'), ({'title': 'x'*1025}, 'title_invalid'),
    ({'description': 'x'*32769}, 'description_invalid'), ({'due_date': '2026-02-30'}, 'due_date_invalid'),
    ({'due_date': '20261012'}, 'due_date_invalid'), ({'board_id': 99}, 'forbidden'),
    ({'column_id': 999}, 'target_invalid'), ({'lane_id': 999}, 'target_invalid'),
    ({'responsible': 'Nobody'}, 'responsible_not_found')])
def test_validation_never_creates(creation, options, code):
    adapter, state = creation
    args = dict(title='Task', board_id=12, column_id=4); args.update(options)
    with pytest.raises(KaitenError) as exc:
        adapter.prepare_card(**args)
    assert exc.value.code == code and not state['cards']
    if code == 'forbidden':
        assert not state['calls']


def test_multiple_lanes_require_selection(creation):
    adapter, state = creation; state['multi_lane'] = True
    with pytest.raises(KaitenError): create(adapter)
    assert not state['cards']
    assert create(adapter, lane_id=6)['card']['lane_id'] == 6


def test_duplicate_or_inactive_responsible_is_not_selected(creation):
    adapter, state = creation
    state['users'].append(dict(id=8, full_name='Иван Иванов', username='other'))
    with pytest.raises(KaitenError) as exc: create(adapter, responsible='Иван Иванов')
    assert exc.value.code == 'responsible_ambiguous' and not state['cards']
    state['users'][0]['activated'] = False
    with pytest.raises(KaitenError) as exc: create(adapter, responsible='7')
    assert exc.value.code == 'responsible_not_found'


def test_preview_authorization_and_token_binding(creation):
    adapter, state = creation
    preview = adapter.prepare_card('Task', board_id=12, column_id=4)
    for kwargs in ({}, {'authorization': 'approved', 'approval': 'wrong'}):
        with pytest.raises(KaitenError): adapter.create_card('Task', board_id=12, column_id=4, **kwargs)
    with pytest.raises(KaitenError):
        adapter.create_card('Changed', board_id=12, column_id=4, authorization='approved', approval=preview['approval'])
    adapter.credentials.save_credentials('work', 'example.kaiten.ru', 'changed-token')
    with pytest.raises(KaitenError) as exc:
        adapter.create_card('Task', board_id=12, column_id=4, authorization='approved', approval=preview['approval'])
    assert exc.value.code == 'preview_changed' and not state['cards']


@pytest.mark.parametrize('status,expected', [(400,'not-applied'), (402,'not-applied'), (403,'not-applied'), (429,'not-applied'), (503,'ambiguous')])
def test_http_failure_is_one_post(creation, status, expected):
    adapter, state = creation; state['status'] = status
    result = create(adapter)
    assert result['status'] == expected and not result['retry_safe']
    assert sum(c[0] == 'POST' for c in state['calls']) == 1
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('failure', ['missing_id', 'wrong_reply_board', 'wrong_readback_id', 'changed_title', 'readback_failure', 'wrong_responsible'])
def test_existing_card_is_retained_on_uncertain_readback(creation, failure):
    adapter, state = creation
    if failure == 'missing_id': state['missing_id'] = True
    if failure == 'wrong_reply_board': state['reply'] = dict(board_id=99)
    if failure == 'wrong_readback_id': state['readback'] = dict(id=999)
    if failure == 'changed_title': state['readback'] = dict(title='Other')
    if failure == 'readback_failure': state['readback_status'] = 503
    if failure == 'wrong_responsible': state['no_responsible'] = True
    result = create(adapter, responsible='7')
    assert result['status'] == 'ambiguous' and len(state['cards']) == 1
    assert sum(c[0] == 'POST' for c in state['calls']) == 1
    if failure in {'missing_id', 'wrong_reply_board'}:
        assert not any(c[1].startswith('/cards/') for c in state['calls'])
    else:
        assert result['card_id'] == 100 and result['url'].endswith('/100')


def test_timeout_after_create_does_not_search_by_title_or_repeat(creation):
    adapter, state = creation; original = adapter._write_once
    def timeout(*args):
        original(*args)
        return None, KaitenError('network', 'Synthetic timeout')
    adapter._write_once = timeout
    result = create(adapter)
    assert result['status'] == 'ambiguous' and len(state['cards']) == 1
    assert sum(c[0] == 'POST' for c in state['calls']) == 1
    assert not any(c[1].startswith('/cards/') for c in state['calls'])


def test_policy_change_during_inspection_blocks_post(creation):
    adapter, state = creation; original = adapter._topology
    def changed(policy, board_id):
        value = original(policy, board_id)
        adapter.store.save(profile='work', tenant='example.kaiten.ru', board_allowlist=[99])
        return value
    adapter._topology = changed
    with pytest.raises(KaitenError) as exc: create(adapter)
    assert exc.value.code == 'policy_changed' and not state['cards']


def test_installed_create_dry_run_approved_and_nonzero_uncertainty(installed_surface, tmp_path):
    driver = tmp_path / 'driver.py'
    driver.write_text('''import runpy,sys
from urllib.parse import urlsplit
from urllib.request import Request,build_opener
from kaiten_cli.client import KaitenClient,NoRedirect
origin,binary=sys.argv[1:3]
original=KaitenClient.__init__
opener=build_opener(NoRedirect())
def transport(request,**kwargs):
 p=urlsplit(request.full_url)
 return opener.open(Request(origin+p.path+('?' + p.query if p.query else ''),data=request.data,method=request.method,headers=dict(request.headers)),**kwargs)
def init(self,credentials,**kwargs): original(self,credentials,open_request=transport)
KaitenClient.__init__=init
sys.argv=[binary,*sys.argv[3:]]
runpy.run_path(binary,run_name='__main__')
''')
    env = {k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME','CLI_ALL_ROOT'} and not k.startswith('KAITEN_')}
    env.update(XDG_CONFIG_HOME=str(tmp_path / 'personal'), PATH=str(tmp_path))
    with creation_api() as (origin, state):
        def invoke(*args, code=0, input=None):
            reply = subprocess.run([str(installed_surface / 'python'), str(driver), origin,
                str(installed_surface / 'kaiten'), '--json', *args], env=env, input=input,
                cwd=tmp_path, capture_output=True, text=True, timeout=10)
            assert reply.returncode == code, reply.stdout + reply.stderr
            assert 'PRIVATE' not in reply.stdout and 'synthetic-create' not in reply.stdout
            return json.loads(reply.stdout)
        invoke('auth','login','--profile','work','--tenant','example.kaiten.ru','--token-stdin',input='synthetic-create\n')
        invoke('setup','--profile','work','--tenant','example.kaiten.ru','--board','12','--confirm')
        args = ('cards','create','--title','Task','--board','12','--column','4','--description','Details','--responsible','@ivan')
        preview = invoke(*args,'--dry-run')['data']
        assert preview['dry_run'] and not state['cards']
        invoke(*args, code=1)
        assert not state['cards']
        result = invoke(*args,'--authorization','approved','--approval',preview['approval'])
        assert result['data']['status'] == 'applied' and len(state['cards']) == 1
        state['missing_id'] = True
        result = invoke(*args,'--authorization','direct',code=1)
        assert result['error']['code'] == 'mutation_ambiguous' and len(state['cards']) == 2
