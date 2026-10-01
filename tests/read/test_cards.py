"""Scope and pagination through our own HTTP client."""

import pytest
from urllib.error import URLError

from kaiten_cli.policy import KaitenError
from kaiten_cli.read import KaitenAdapter

CARD = {'id': 33, 'title': 'Задача', 'board_id': 12, 'column_id': 4, 'lane_id': 5,
        'state': 1, 'description': 'Описание', 'members': [{'email': 'omit@example.invalid'}]}


def test_policy_denial_has_no_http_request_or_config_change(fake_api, local_store):
    adapter = KaitenAdapter(local_store, **fake_api({}))
    before = local_store.path.read_bytes()
    with pytest.raises(KaitenError) as failure:
        adapter.find_cards('33', board_id=99)
    assert failure.value.code == 'forbidden'
    assert local_store.path.read_bytes() == before
    local_store.path.write_text('{"profile":"test-profile","tenant":"example.kaiten.ru","board_allowlist":[]}')
    with pytest.raises(KaitenError) as failure:
        adapter.find_cards('Задача')
    assert failure.value.code == 'allowlist_empty' and fake_api.calls() == []


@pytest.mark.parametrize('query', ['33', 'https://example.kaiten.ru/space/2/boards/card/33', 'Задача; $(touch NEVER)'])
def test_ids_urls_and_unicode_use_scoped_http_params(query, fake_api, local_store):
    result = KaitenAdapter(local_store, **fake_api({'/cards': [CARD]})).find_cards(query, board_id=12)
    assert result == {'status': 'found', 'cards': [{k: v for k, v in CARD.items() if k != 'members'}], 'complete': True}
    calls = fake_api.calls()
    params = {'board_id': ['12'], 'limit': ['100'], 'offset': ['0'], 'additional_card_fields': ['description']}
    params.update({'query': [query]} if query.startswith('Задача') else {'ids': ['33']})
    assert calls == [{'method': 'GET', 'path': '/cards', 'params': params, 'body': None}]


@pytest.mark.parametrize('reply,code', [({'status': 401}, 'auth'), ({'status': 403}, 'forbidden'),
    ({'status': 503}, 'network'), ({'status': 429}, 'rate_limited'), (URLError('SECRET'), 'network'),
    ({'raw': 'warning before JSON\n{}'}, 'bad_json'), ('not-a-list', 'bad_json')])
def test_failure_taxonomy_is_safe_and_never_retried(reply, code, fake_api, local_store):
    with pytest.raises(KaitenError) as failure:
        KaitenAdapter(local_store, **fake_api({'/cards': reply})).find_cards('Задача')
    assert failure.value.code == code
    assert 'SECRET' not in str(failure.value) + str(failure.value.details)
    assert len(fake_api.calls()) == 1


def test_scoped_lookup_follows_pages(fake_api, local_store):
    backend = fake_api({('GET', '/cards', '0'): [{'id': i, 'title': 'Другая', 'board_id': 12} for i in range(1, 101)],
                        ('GET', '/cards', '100'): [{'id': 777, 'title': 'Искомая', 'board_id': 12}]})
    result = KaitenAdapter(local_store, **backend).find_cards('777')
    assert result['status'] == 'found' and result['cards'][0]['id'] == 777 and result['complete']
    assert len(fake_api.calls()) == 2


@pytest.mark.parametrize('query,domain,payload,code,count', [
    ('https://other.kaiten.ru/space/2/boards/card/33', 'example.kaiten.ru', [], 'forbidden', 0),
    ('33', 'wrong.kaiten.ru', [], 'profile_mismatch', 0),
    ('33', 'example.kaiten.ru', [{'id': 33, 'title': 'Do not disclose', 'board_id': 99}], 'forbidden', 1),
    ('33', 'example.kaiten.ru', [], None, 1)])
def test_empty_and_scope_failures_are_distinct(query, domain, payload, code, count, fake_api, local_store):
    fake_api.credentials.save_credentials('test-profile', domain, 'synthetic-fixture-token')
    adapter = KaitenAdapter(local_store, **fake_api({'/cards': payload}))
    if code:
        with pytest.raises(KaitenError) as failure:
            adapter.find_cards(query)
        assert failure.value.code == code and 'Do not disclose' not in str(failure.value)
    else:
        assert adapter.find_cards(query) == {'status': 'empty', 'cards': [], 'complete': True}
    assert len(fake_api.calls()) == count
