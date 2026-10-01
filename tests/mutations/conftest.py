"""Synthetic direct API seam for authorization and readback behavior."""

import pytest
from kaiten_cli.auth import CredentialStore
from kaiten_cli.mutations import KaitenMutations
from kaiten_cli.policy import KaitenError, LocalConfigStore


class Backend:
    def __init__(self):
        self.calls = []
        self.card = {'id': 33, 'title': 'Задача', 'board_id': 12, 'column_id': 4,
                     'lane_id': 5, 'state': 1, 'description': 'Описание'}
        self.users = [{'id': 7, 'full_name': 'Иван Иванов', 'username': 'иван', 'email': 'omit@example.invalid'}]
        self.comments = [{'id': 1, 'card_id': 33, 'author_id': 7, 'text': 'Старый', 'internal': True}]
        self.reply = None
        self.apply = True
        self.readback_error = False

    def request(self, method, path, *, params=None, body=None):
        self.calls.append({'method': method, 'path': path, 'params': params, 'body': body})
        if self.readback_error and self.writes and method == 'GET' and path in ('/cards', '/cards/33/comments'):
            raise KaitenError('network', 'Synthetic network failure')
        if path == '/cards':
            return [self.card.copy()]
        if path == '/users':
            return self.users
        if path == '/users/current':
            return {'id': 7}
        if method == 'GET' and path == '/cards/33/comments':
            return self.comments
        if path == '/boards/12/columns':
            return [{'id': 4, 'title': 'Очередь', 'board_id': 12}, {'id': 6, 'title': 'Работа', 'board_id': 12}]
        if path == '/boards/12/lanes':
            return [{'id': 5, 'title': 'Основная', 'board_id': 12}]
        if method == 'POST' and path == '/cards/33/comments':
            assert set(body) == {'text', 'type', 'internal'} and body['internal'] is True and body['type'] == 1
            data = {'id': 2, 'card_id': 33, 'author_id': 7, 'text': body['text'], 'internal': True}
            if self.apply:
                self.comments.append(data)
        elif method == 'PATCH' and path == '/cards/33':
            assert set(body) == {'column_id', 'lane_id'}
            if self.apply:
                self.card.update(body)
            data = self.card.copy()
        else:
            raise AssertionError((method, path))
        if self.reply:
            raise self.reply
        return data

    @property
    def writes(self):
        return [call for call in self.calls if call['method'] != 'GET']


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    monkeypatch.delenv('KAITEN_TOKEN', raising=False)
    store = LocalConfigStore(tmp_path / 'personal/kaiten.json')
    store.save(profile='mpstats', tenant='mpstats.kaiten.ru', board_allowlist=[12])
    credentials = CredentialStore(tmp_path / 'personal/profiles.json')
    credentials.save_credentials('mpstats', 'mpstats.kaiten.ru', 'synthetic-fixture-token')
    backend = Backend()
    return KaitenMutations(store, credentials=credentials, client_factory=lambda selected: backend), backend
