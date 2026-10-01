"""Synthetic responses at our direct HTTP transport seam; never company data."""

import io
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from kaiten_cli.auth import CredentialStore
from kaiten_cli.client import KaitenClient
from kaiten_cli.policy import LocalConfigStore


class Response(io.BytesIO):
    status = 200


@pytest.fixture
def local_store(tmp_path):
    store = LocalConfigStore(tmp_path / 'personal/policy.json')
    store.save(profile='test-profile', tenant='example.kaiten.ru', board_allowlist=[12])
    return store


@pytest.fixture
def fake_api(tmp_path, monkeypatch):
    monkeypatch.delenv('KAITEN_TOKEN', raising=False)
    credentials = CredentialStore(tmp_path / 'personal/profiles.json')
    credentials.save_credentials('test-profile', 'example.kaiten.ru', 'synthetic-fixture-token')
    calls = []

    def configure(responses):
        def open_request(request, **kwargs):
            parsed = urlsplit(request.full_url)
            call = {'method': request.method, 'path': parsed.path.removeprefix('/api/latest'),
                    'params': parse_qs(parsed.query), 'body': json.loads(request.data) if request.data else None}
            calls.append(call)
            key = (request.method, call['path'], call['params'].get('offset', ['0'])[0])
            reply = responses.get(key, responses.get(call['path']))
            assert reply is not None, call
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, dict) and 'status' in reply:
                raise HTTPError(request.full_url, reply['status'], 'SECRET not echoed', {}, io.BytesIO(b'SECRET'))
            raw = reply['raw'].encode() if isinstance(reply, dict) and 'raw' in reply else json.dumps(reply, ensure_ascii=False).encode()
            return Response(raw)
        return {'credentials': credentials, 'client_factory': lambda selected: KaitenClient(selected, open_request=open_request)}
    configure.calls = lambda: calls
    configure.credentials = credentials
    return configure
