"""Card history uses the direct transport and preserves board scope."""

import json

import pytest
from click.testing import CliRunner

from kaiten_cli import cli
from kaiten_cli.policy import KaitenError
from kaiten_cli.read import KaitenAdapter


CARD = {'id': 33, 'title': 'Карточка', 'board_id': 12, 'column_id': 4, 'lane_id': 5,
        'column': {'id': 4, 'title': 'Готово'}, 'lane': {'id': 5, 'title': 'Разработка'}}
HISTORY_PATH = '/cards/33/location-history'


def event(day, condition, **location):
    return {'id': str(day), 'card_id': 33, 'condition': condition,
            'changed': f'2026-11-{day:02}T13:00:00Z', 'board_id': 12,
            'column_id': 4, 'subcolumn_id': None, 'lane_id': 5, **location}


def test_history_reports_archive_transition_and_origin(monkeypatch, fake_api, local_store):
    card_url = 'https://example.kaiten.ru/boards/card/33'
    before = event(8, 1, subcolumn_id=66)
    archived = event(9, 2)
    archived_move = event(10, 2)
    adapter = KaitenAdapter(local_store, **fake_api({
        '/cards': [CARD], HISTORY_PATH: [archived, archived_move,
                                       {**before, 'author': {'email': 'hidden@example.invalid'}}],
    }))
    monkeypatch.setattr(cli, 'KaitenAdapter', lambda store: adapter)
    reply = CliRunner().invoke(cli.kaiten, ['cards', 'history', card_url, '--board', '12', '--json'])

    assert reply.exit_code == 0
    assert json.loads(reply.output)['data'] == {
        'card_id': 33, 'events': [archived_move, archived, before], 'complete': True,
        'archived_at': archived['changed'],
        'archived_from': {'board_id': 12, 'column_id': 4, 'subcolumn_id': 66, 'lane_id': 5,
                          'column_title': 'Готово', 'lane_title': 'Разработка'},
    }
    assert [(call['method'], call['path']) for call in fake_api.calls()] == [
        ('GET', '/cards'), ('GET', HISTORY_PATH),
    ]


def test_history_resolves_names_on_prior_allowed_board(fake_api, local_store):
    local_store.save(profile='test-profile', tenant='example.kaiten.ru', board_allowlist=[12, 34])
    before = event(8, 1, board_id=34, column_id=7, lane_id=6)
    adapter = KaitenAdapter(local_store, **fake_api({
        '/cards': [CARD], HISTORY_PATH: [event(9, 2), before],
        '/boards/34/columns': [{'id': 7, 'title': 'Раньше', 'board_id': 34}],
        '/boards/34/lanes': [{'id': 6, 'title': 'Другая дорожка', 'board_id': 34}],
    }))

    result = adapter.card_history('33', board_id=12)

    assert result['archived_from'] == {'board_id': 34, 'column_id': 7, 'subcolumn_id': None, 'lane_id': 6,
                                       'column_title': 'Раньше', 'lane_title': 'Другая дорожка'}
    assert result['complete'] is True


def test_history_hides_disallowed_events_and_never_guesses_origin(fake_api, local_store):
    allowed = event(7, 1)
    hidden = event(8, 1, board_id=99, lane_id=99)
    archived = event(9, 2)
    adapter = KaitenAdapter(local_store, **fake_api({
        '/cards': [CARD], HISTORY_PATH: [allowed, hidden, archived],
    }))

    result = adapter.card_history('33', board_id=12)

    assert result['events'] == [archived, allowed]
    assert result['archived_from'] is None and result['complete'] is False
    assert len(fake_api.calls()) == 2


@pytest.mark.parametrize('board,reference,reply,code,count', [
    (99, '33', [], 'forbidden', 0),
    (12, 'текст', [], 'query_invalid', 0),
    (12, '33', [], 'not_found', 2),
])
def test_history_must_resolve_one_card_in_scope(board, reference, reply, code, count, fake_api, local_store):
    adapter = KaitenAdapter(local_store, **fake_api({'/cards': reply}))

    with pytest.raises(KaitenError) as error:
        adapter.card_history(reference, board_id=board)

    assert error.value.code == code
    assert len(fake_api.calls()) == count


def test_history_transport_route_is_read_only(fake_api):
    config = fake_api({})
    credentials = config['credentials'].resolve('test-profile', 'example.kaiten.ru')
    client = config['client_factory'](credentials)

    with pytest.raises(KaitenError) as error:
        client.request('PATCH', HISTORY_PATH)

    assert error.value.code == 'request_invalid'
    assert fake_api.calls() == []
