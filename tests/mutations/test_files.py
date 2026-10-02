"""Binary upload and attachment workflows against a local HTTP server, never Kaiten."""

from contextlib import contextmanager
from email import policy as email_policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from uuid import uuid4

import pytest

from kaiten_cli.auth import CredentialStore, Credentials
from kaiten_cli.client import KaitenClient, NoRedirect
from kaiten_cli.files import KaitenFiles, read_file
from kaiten_cli.policy import KaitenError, LocalConfigStore
from test_full_e2e import installed_surface


CARD_UID = 'c0ffee00-1234-4abc-9def-0123456789ab'
AUTHOR_UID = '2b7c9d41-8e05-4f62-9a37-c1d0b6e845f3'
COMMENT_UID = '5e8b3f21-7c04-4d69-9a12-b8f3e0c7d461'


@contextmanager
def file_api():
    state = {'calls': [], 'uploads': [], 'stored': {}, 'files': [], 'fail': {},
             'board': 12, 'uid': CARD_UID, 'readback_fail': False, 'reply_override': {}, 'readback_override': {},
             'comments': [{'id': 1, 'uid': COMMENT_UID, 'card_id': 33, 'text': 'Старый',
                           'internal': True, 'author_id': 7, 'deleted': False}]}

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

        def do_GET(self):
            path = urlsplit(self.path).path.removeprefix('/api/latest')
            state['calls'].append(('GET', path))
            card = {'id': 33, 'uid': state['uid'], 'title': 'Задача', 'board_id': state['board'],
                    'column_id': 4, 'lane_id': 5, 'state': 1, 'description': '', 'files': state['files']}
            if path == '/cards':
                return self.respond(200, [card])
            if path == '/cards/33':
                return self.respond(200, card)
            if path == '/cards/33/comments':
                return self.respond(200, state['comments'])
            if path == '/users/current':
                return self.respond(200, {'id': 7, 'uid': AUTHOR_UID})
            if path == '/users':
                return self.respond(200, [{'id': 7, 'full_name': 'Иван Иванов', 'username': 'ivan'}])
            if path in state['stored']:
                if state['readback_fail']:
                    return self.respond(503, {'message': 'PRIVATE response'})
                # A presigned link must never leak into CLI output or be followed.
                return self.respond(200, {**state['stored'][path], **state['readback_override'], 'url': 'https://example.invalid/SIGNED_SECRET'})
            return self.respond(404, {})

        def do_POST(self):
            path = urlsplit(self.path).path.removeprefix('/api/latest')
            state['calls'].append(('POST', path))
            raw = self.rfile.read(int(self.headers['Content-Length']))
            assert self.headers['Authorization'].startswith('Bearer synthetic-')
            if path == '/cards/33/comments':
                body = json.loads(raw)
                assert set(body) == {'text', 'type', 'internal'} and body['internal'] is True
                comment = {'id': len(state['comments']) + 1, 'uid': str(uuid4()), 'card_id': 33,
                           'author_id': 7, 'text': body['text'], 'internal': True, 'deleted': False}
                state['comments'].append(comment)
                return self.respond(200, comment)
            message = BytesParser(policy=email_policy.default).parsebytes(
                ('Content-Type: ' + self.headers['Content-Type'] + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + raw)
            parts = list(message.iter_parts())
            assert len(parts) == 1 and parts[0].get_param('name', header='Content-Disposition') == 'file'
            filename, data = parts[0].get_filename(), parts[0].get_payload(decode=True)
            state['uploads'].append({'path': path, 'name': filename, 'data': data})
            status = state['fail'].get(filename)
            if status:
                return self.respond(status, {'message': 'PRIVATE token should not appear'})
            entity = 'comment' if '/comments/' in path else 'card'
            item = {'id': str(uuid4()), 'name': filename, 'size': str(len(data)), 'mime_type': parts[0].get_content_type(),
                    'author_uid': AUTHOR_UID, 'card_uid': CARD_UID, 'entity_type': entity}
            if entity == 'comment':
                comment_uid = path.split('/comments/')[1].split('/')[0]
                assert any(c['uid'] == comment_uid and c['internal'] is True for c in state['comments'])
                item['comment_uid'] = comment_uid
                next(c for c in state['comments'] if c['uid'] == comment_uid).setdefault('attacments', []).append({**item, 'type': 11, 'url': 'https://example.invalid/SIGNED_SECRET'})
            else:
                assert path == f'/cards/{CARD_UID}/files'
            state['files'].append({**item, 'type': 11})
            state['stored'][path + '/' + item['id']] = item
            self.respond(200, {**item, **state['reply_override']})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown(); thread.join(timeout=3); server.server_close()


def local_transport(origin):
    opener = build_opener(NoRedirect())
    def send(request, **kwargs):
        parsed = urlsplit(request.full_url)
        return opener.open(Request(origin + parsed.path + ('?' + parsed.query if parsed.query else ''),
                           data=request.data, method=request.method, headers=dict(request.headers)), **kwargs)
    return send


@pytest.fixture
def file_workflow(tmp_path, monkeypatch):
    monkeypatch.delenv('KAITEN_TOKEN', raising=False)
    monkeypatch.delenv('KAITEN_DOMAIN', raising=False)
    store = LocalConfigStore(tmp_path / 'personal/policy.json')
    store.save(profile='work', tenant='example.kaiten.ru', board_allowlist=[12])
    credentials = CredentialStore(tmp_path / 'personal/profiles.json')
    credentials.save_credentials('work', 'example.kaiten.ru', 'synthetic-upload-token')
    with file_api() as (origin, state):
        adapter = KaitenFiles(store, credentials=credentials,
                             client_factory=lambda c: KaitenClient(c, open_request=local_transport(origin)))
        yield adapter, state


@pytest.mark.parametrize('comment_id', [None, 1])
def test_binary_upload_readback_utf8_filename_and_preview(file_workflow, tmp_path, comment_id):
    adapter, state = file_workflow
    source = tmp_path / 'отчёт "final".pdf'
    source.write_bytes(b'%PDF\x00\xff\r\nbytes')
    preview = adapter.prepare_file('33', source, comment_id=comment_id)
    assert not state['uploads']
    result = adapter.upload_file('33', source, comment_id=comment_id,
                                 authorization='approved', approval=preview['approval'])
    assert result['status'] == 'applied' and result['readback'] and result['attempts'] == 1
    expected = f'/cards/{CARD_UID}' + (f'/comments/{COMMENT_UID}' if comment_id else '') + '/files'
    assert state['uploads'] == [{'path': expected, 'name': source.name, 'data': source.read_bytes()}]
    assert 'SIGNED_SECRET' not in json.dumps(result) and 'synthetic-upload-token' not in json.dumps(result)


def test_changed_bytes_same_size_and_changed_target_block_before_upload(file_workflow, tmp_path):
    adapter, state = file_workflow
    path = tmp_path / 'x.bin'; path.write_bytes(b'abcd')
    preview = adapter.prepare_file('33', path)
    path.write_bytes(b'dcba')
    with pytest.raises(KaitenError) as exc:
        adapter.upload_file('33', path, authorization='approved', approval=preview['approval'])
    assert exc.value.code == 'preview_changed' and not state['uploads']
    path.write_bytes(b'abcd'); state['uid'] = str(uuid4())
    with pytest.raises(KaitenError) as exc:
        adapter.upload_file('33', path, authorization='approved', approval=preview['approval'])
    assert exc.value.code == 'preview_changed' and not state['uploads']


@pytest.mark.parametrize('failure', ['board', 'external', 'uid', 'missing_comment', 'credentials'])
def test_scope_internal_comment_uid_and_credential_guards(file_workflow, tmp_path, failure):
    adapter, state = file_workflow
    path = tmp_path / 'report.pdf'; path.write_bytes(b'report')
    preview = adapter.prepare_file('33', path, comment_id=1)
    expected = {'board': 'forbidden', 'external': 'comment_external', 'uid': 'uid_required',
                'missing_comment': 'comment_not_found', 'credentials': 'preview_changed'}[failure]
    if failure == 'board': state['board'] = 99
    if failure == 'external': state['comments'][0]['internal'] = False
    if failure == 'uid': state['uid'] = '33'
    if failure == 'missing_comment': state['comments'][0]['deleted'] = True
    if failure == 'credentials': adapter.credentials.save_credentials('work', 'example.kaiten.ru', 'synthetic-changed-token')
    with pytest.raises(KaitenError) as exc:
        adapter.upload_file('33', path, comment_id=1, authorization='approved', approval=preview['approval'])
    assert exc.value.code == expected and not state['uploads']


@pytest.mark.parametrize('status,expected', [(403, 'not-applied'), (413, 'not-applied'), (503, 'ambiguous')])
def test_http_failure_never_retries_or_falls_back(file_workflow, tmp_path, status, expected):
    adapter, state = file_workflow
    path = tmp_path / 'fail.bin'; path.write_bytes(b'hello')
    state['fail'][path.name] = status
    result = adapter.upload_file('33', path, authorization='direct')
    assert result['status'] == expected and result['retry_safe'] is False
    assert len(state['uploads']) == 1
    assert state['uploads'][0]['path'] == f'/cards/{CARD_UID}/files'
    assert 'PRIVATE' not in json.dumps(result)


def test_failed_readback_is_ambiguous_even_after_upload_success(file_workflow, tmp_path):
    adapter, state = file_workflow
    path = tmp_path / 'ok.txt'; path.write_bytes(b'hello')
    state['readback_fail'] = True
    result = adapter.upload_file('33', path, authorization='direct')
    assert result['status'] == 'ambiguous' and not result['readback']
    assert len(state['uploads']) == 1 and len(state['files']) == 1


def test_comment_with_two_files_and_mention_preserves_internal_workflow(file_workflow, tmp_path):
    adapter, state = file_workflow
    paths = [tmp_path / n for n in ('one.pdf', 'two.xlsx')]
    for n, path in enumerate(paths): path.write_bytes(bytes([n, 0, 255]))
    preview = adapter.prepare_comment('33', 'Готово', mention='Иван Иванов', file_paths=paths)
    result = adapter.post_comment('33', 'Готово', mention='Иван Иванов', file_paths=paths, approval=preview['approval'])
    assert result['status'] == 'applied' and len(result['files']) == 2
    assert result['comment']['internal'] is True and result['comment']['text'] == '@ivan Готово'
    assert len(state['comments']) == 2 and len(state['uploads']) == 2
    assert all(result['comment']['uid'] in u['path'] for u in state['uploads'])


def test_partial_comment_does_not_repeat_text_or_continue_uploads(file_workflow, tmp_path):
    adapter, state = file_workflow
    paths = [tmp_path / n for n in ('one.txt', 'fail.txt', 'three.txt')]
    for path in paths: path.write_bytes(b'hello')
    state['fail']['fail.txt'] = 403
    preview = adapter.prepare_comment('33', 'Готово', mention='нет', file_paths=paths)
    result = adapter.post_comment('33', 'Готово', mention='нет', file_paths=paths, approval=preview['approval'])
    assert result['status'] == 'partial' and len(state['comments']) == 2
    assert len(result['files']) == 1 and result['comment']['id'] == 2
    assert result['failed_file']['name'] == 'fail.txt' and result['remaining_files'][0]['name'] == 'three.txt'
    assert len(state['uploads']) == 2 and result['retry_safe'] is False


def test_comment_attachment_change_blocks_comment_creation(file_workflow, tmp_path):
    adapter, state = file_workflow
    path = tmp_path / 'x.txt'; path.write_bytes(b'first')
    preview = adapter.prepare_comment('33', 'Готово', mention='нет', file_paths=[path])
    path.write_bytes(b'other')
    with pytest.raises(KaitenError) as exc:
        adapter.post_comment('33', 'Готово', mention='нет', file_paths=[path], approval=preview['approval'])
    assert exc.value.code == 'preview_changed' and len(state['comments']) == 1 and not state['uploads']


def test_invalid_local_files_and_legacy_upload_routes_fail(tmp_path):
    with pytest.raises(KaitenError): read_file(tmp_path)
    pipe = tmp_path / 'pipe'; os.mkfifo(pipe)
    with pytest.raises(KaitenError): read_file(pipe)
    huge = tmp_path / 'huge'
    with huge.open('wb') as handle: handle.truncate(100 * 1024 * 1024 + 1)
    with pytest.raises(KaitenError) as exc: read_file(huge)
    assert exc.value.code == 'file_too_large'
    client = KaitenClient(Credentials('work', 'example.kaiten.ru', 'synthetic-token'),
                          open_request=lambda *a, **k: pytest.fail('No request should be made'))
    for path in ['/cards/33/files', f'/cards/{CARD_UID}/comments/new/files', 'https://example.invalid/files']:
        with pytest.raises(KaitenError): client.upload_file(path, name='file.txt', data=b'x', mime_type='text/plain')
    with pytest.raises(KaitenError): client.upload_file(f'/cards/{CARD_UID}/files', name='evil\r\n.txt', data=b'x', mime_type='text/plain')


def test_wheel_installed_cli_uploads_and_partial_nonzero(installed_surface, tmp_path):
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
    env = {k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME', 'CLI_ALL_ROOT'} and not k.startswith('KAITEN_')}
    env.update(XDG_CONFIG_HOME=str(tmp_path / 'personal'), PATH=str(tmp_path))
    path = tmp_path / 'report.pdf'; path.write_bytes(b'%PDF\x00\xff')
    with file_api() as (origin, state):
        def invoke(*args, code=0, input=None):
            reply = subprocess.run([str(installed_surface / 'python'), str(driver), origin,
                                   str(installed_surface / 'kaiten'), '--json', *args],
                                  env=env, input=input, cwd=tmp_path, capture_output=True, text=True, timeout=10)
            assert reply.returncode == code, reply.stdout + reply.stderr
            assert 'SIGNED_SECRET' not in reply.stdout and 'synthetic-installed' not in reply.stdout
            return json.loads(reply.stdout)
        invoke('auth', 'login', '--profile', 'work', '--tenant', 'example.kaiten.ru', '--token-stdin', input='synthetic-installed\n')
        invoke('setup', '--profile', 'work', '--tenant', 'example.kaiten.ru', '--board', '12', '--confirm')
        preview = invoke('files', 'prepare', '33', '--file', str(path))['data']
        invoke('files', 'upload', '33', '--file', str(path), '--authorization', 'approved', '--approval', preview['approval'], '--dry-run')
        assert not state['uploads']
        result = invoke('files', 'upload', '33', '--file', str(path), '--authorization', 'approved', '--approval', preview['approval'])
        assert result['data']['readback'] and len(state['uploads']) == 1
        args = ('33', '--text', 'Отчёт готов', '--mention', 'нет', '--file', str(path))
        preview = invoke('comments', 'prepare', *args)['data']
        state['fail'][path.name] = 413
        result = invoke('comments', 'post', *args, '--approval', preview['approval'], code=1)
        assert result['error']['code'] == 'mutation_partial'
        assert result['error']['details']['comment']['id'] == 2 and len(state['comments']) == 2


@pytest.mark.parametrize('side', ['reply', 'readback'])
def test_wrong_target_metadata_is_not_success(file_workflow, tmp_path, side):
    adapter, state = file_workflow
    path = tmp_path / 'target.txt'; path.write_bytes(b'bytes')
    state[side + '_override'] = {'card_uid': str(uuid4())}
    result = adapter.upload_file('33', path, authorization='direct')
    assert result['status'] == 'ambiguous' and not result['readback']
    assert len(state['uploads']) == 1
    assert len(adapter.list_files('33')['files']) == 1


def test_list_handles_old_and_new_attachments_without_urls(file_workflow, tmp_path):
    adapter, state = file_workflow
    state['files'].append({'id': 123, 'name': 'old.pdf', 'size': 3, 'type': 1, 'url': 'https://example.invalid/PRIVATE_URL'})
    path = tmp_path / 'new.pdf'; path.write_bytes(b'PDF')
    adapter.upload_file('33', path, comment_id=1, authorization='direct')
    result = adapter.list_files('33', comment_id=1)
    assert result['files'][0]['type'] == 11 and result['files'][0]['name'] == 'new.pdf'
    assert adapter.list_files('33')['files'][0]['type'] == 1
    assert 'SIGNED_SECRET' not in json.dumps(result) and 'PRIVATE_URL' not in json.dumps(adapter.list_files('33'))


def test_denied_board_does_not_read_local_file_or_call_http(file_workflow, tmp_path):
    adapter, state = file_workflow
    with pytest.raises(KaitenError) as exc:
        adapter.prepare_file('33', tmp_path / 'absent', board_id=99)
    assert exc.value.code == 'forbidden' and not state['calls']


def test_more_than_ten_and_duplicate_files_fail_before_comment_creation(file_workflow, tmp_path):
    adapter, state = file_workflow
    path = tmp_path / 'x'; path.write_bytes(b'x')
    for paths in ([path] * 11, [path, path]):
        with pytest.raises(KaitenError) as exc:
            adapter.prepare_comment('33', 'Готово', mention='нет', file_paths=paths)
        assert exc.value.code == 'files_invalid' and len(state['comments']) == 1 and not state['uploads']
