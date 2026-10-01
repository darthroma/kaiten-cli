"""Our HTTP client against a local synthetic server; never the company API."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener

import pytest

from kaiten_cli.auth import Credentials
from kaiten_cli.client import KaitenClient, NoRedirect
from kaiten_cli.policy import KaitenError


@contextmanager
def local_api():
    calls = []
    state = {'column': 4, 'comments': []}

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path.removeprefix('/api/latest')
            calls.append(('GET', self.path, None, self.headers.get('Authorization')))
            card = {'id': 33, 'title': 'Задача', 'board_id': 12, 'column_id': state['column'], 'lane_id': 5,
                    'state': 1, 'description': 'Описание'}
            data = {'/spaces': [{'id': 2, 'title': 'Team'}], '/spaces/2/boards': [{'id': 12, 'title': 'Work'}],
                    '/cards': [card], '/users/current': {'id': 7}, '/cards/33/comments': state['comments'],
                    '/boards/12/columns': [{'id': 4, 'title': 'Очередь'}, {'id': 6, 'title': 'Работа'}],
                    '/boards/12/lanes': [{'id': 5, 'title': 'Основная'}]}
            if path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/api/latest/secret')
                self.end_headers()
                return
            self.respond(200, data.get(path, []))

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(('POST', self.path, body, self.headers.get('Authorization')))
            data = {'id': 8, 'card_id': 33, 'author_id': 7, 'text': body['text'], 'internal': body['internal']}
            state['comments'].append(data)
            self.respond(200, data)

        def do_PATCH(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(('PATCH', self.path, body, self.headers.get('Authorization')))
            if body['column_id'] == 999:
                self.respond(503, {'message': 'PRIVATE token-like error never echoed'})
                return
            state['column'] = body['column_id']
            self.respond(200, {'id': 33, **body})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_native_client_internal_json_patch_body_and_no_retry():
    with local_api() as (origin, calls):
        opener = build_opener(NoRedirect())
        def local_transport(request, **kwargs):
            local = Request(origin + urlsplit(request.full_url).path, data=request.data,
                            method=request.method, headers=dict(request.headers))
            return opener.open(local, **kwargs)
        client = KaitenClient(Credentials('fixture', 'example.kaiten.ru', 'synthetic-http-fixture'), open_request=local_transport)
        created = client.request('POST', '/cards/33/comments', body={'text': 'Готово', 'type': 1, 'internal': True})
        assert created['internal'] is True
        client.request('PATCH', '/cards/33', body={'column_id': 6, 'lane_id': 5})
        with pytest.raises(KaitenError) as error:
            client.request('PATCH', '/cards/33', body={'column_id': 999, 'lane_id': 5})
        assert error.value.code == 'network' and error.value.details['status_code'] == 503
        assert 'PRIVATE' not in str(error.value) + str(error.value.details)
        with pytest.raises(KaitenError) as redirect:
            client.request('GET', '/redirect')
        assert redirect.value.code == 'redirect_blocked'
    assert len(calls) == 4
    assert calls[0][:3] == ('POST', '/api/latest/cards/33/comments', {'text': 'Готово', 'type': 1, 'internal': True})
    assert calls[1][:3] == ('PATCH', '/api/latest/cards/33', {'column_id': 6, 'lane_id': 5})
    assert sum(1 for _, _, body, _ in calls if body and body.get('column_id') == 999) == 1
    assert all(auth == 'Bearer synthetic-http-fixture' for _, _, _, auth in calls)
    assert not any('/secret' in path for _, path, _, _ in calls)
