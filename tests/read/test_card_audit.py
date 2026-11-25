import json

from click.testing import CliRunner

from kaiten_cli import cli
from kaiten_cli.audit import KaitenAudit


CUTOFF = '2026-11-24T07:13:35Z'
CREATED = '2026-11-24T12:08:36.865Z'


def _card(card_id, board=12, **fields):
    return {'id': card_id, 'board_id': board, 'title': 'Card', 'condition': 1,
            'created': CREATED, 'parents': [], 'children': [], **fields}


def _setup(monkeypatch, fake_api, local_store, responses):
    local_store.save(profile='test-profile', tenant='example.kaiten.ru', board_allowlist=[12, 34, 56])
    adapter = KaitenAudit(local_store, **fake_api(responses))
    monkeypatch.setattr(cli, 'KaitenAdapter', lambda store: adapter)
    monkeypatch.setattr(cli, 'KaitenAudit', lambda store: adapter)


def _run(*args, exit_code=0):
    result = CliRunner().invoke(cli.kaiten, ['cards', *args, '--json'])
    assert result.exit_code == exit_code, (result.output, result.exception)
    return json.loads(result.output)


def test_compact_search_keeps_explicit_boards_and_filters_cutoff_locally(monkeypatch, fake_api, local_store):
    def cards(params):
        if params['board_id'] == ['12']:
            return [_card(21, created='2026-11-24T10:13:35+03:00'), _card(22, description='private'),
                    _card(22), _card(24, created='2026-11-19T11:16:06.470Z')]
        return [_card(23, 34)]

    _setup(monkeypatch, fake_api, local_store, {'/cards': cards})
    result = _run('search', '--compact', '--board', '12', '--board', '34', '--board', '12',
                  '--condition', 'active', '--created-after', CUTOFF)
    assert result['data'] == {'complete': True, 'boards': [
        {'board_id': 12, 'card_ids': [22], 'count': 1, 'complete': True},
        {'board_id': 34, 'card_ids': [23], 'count': 1, 'complete': True},
    ]}
    assert [call['params'] for call in fake_api.calls()] == [
        {'board_id': [board], 'offset': ['0'], 'limit': ['100'], 'condition': ['1'],
         'created_after': ['2026-11-24T07:13:35+00:00']}
        for board in ('12', '34')]


def test_compact_search_continues_after_one_board_fails(monkeypatch, fake_api, local_store):
    _setup(monkeypatch, fake_api, local_store, {
        '/cards': lambda params: {'status': 503} if params['board_id'] == ['12'] else [_card(23, 34)],
    })
    result = _run('search', '--compact', '--board', '12', '--board', '34', '--condition', 'active', exit_code=1)
    assert result['error']['code'] == 'incomplete'
    assert result['error']['details'] == {'complete': False, 'boards': [
        {'board_id': 12, 'card_ids': [], 'count': 0, 'complete': False, 'error': 'network'},
        {'board_id': 34, 'card_ids': [23], 'count': 1, 'complete': True},
    ]}


def test_audit_options_validate_before_http(monkeypatch, fake_api, local_store):
    _setup(monkeypatch, fake_api, local_store, {})
    assert _run('audit-relations', '--board', '12', '--ancestor-board', '99', exit_code=1)['error']['code'] == 'forbidden'
    assert _run('search', '--compact', '--board', '12', '--board', '99', '--condition', 'active',
                exit_code=1)['error']['code'] == 'forbidden'
    assert _run('audit-relations', '--board', '12', '--creator', '7', exit_code=1)['error']['code'] == 'query_invalid'
    assert _run('search', '--created-after', '2026-11-24T07:13:35', exit_code=1)['error']['code'] == 'query_invalid'
    assert fake_api.calls() == []


def _event(card_id, author_id, **fields):
    return {'action': 'card_add', 'card_id': card_id, 'board_id': 12, 'author_id': author_id,
            'created': CREATED, **fields}


def test_view_verifies_creator_from_scoped_cursor_pages(monkeypatch, fake_api, local_store):
    card = _card(21, owner_id=5)

    def activity(params):
        if 'cursor' in params:
            return {'items': [_event(21, 7, author={'email': 'private@example.invalid'})], 'next_cursor': None}
        return {'items': [_event(21, 99, board_id=99), _event(22, 98),
                          _event(21, 97, action='card_move'), _event(21, 96, created='2026-11-25T12:00:00Z')],
                'next_cursor': 'second-page'}

    _setup(monkeypatch, fake_api, local_store, {'/cards': [card], '/cards/21': card, '/spaces/8/activity': activity})
    result = _run('view', 'https://example.kaiten.ru/space/8/boards/card/21', '--with-creator', '--board', '12')
    row = result['data']['cards'][0]
    assert row['owner_id'] == 5 and row['creator_id'] == 7 and row['creator_status'] == 'verified'
    assert 'private@example.invalid' not in json.dumps(result)
    assert fake_api.calls()[-2]['params']['actions'] == ['card_add']
    assert fake_api.calls()[-1]['params']['cursor'] == ['second-page']


def test_audit_distinguishes_a_missing_parent_from_a_broken_or_hidden_chain(monkeypatch, fake_api, local_store):
    target = _card(41, 56, condition=2, archived=True)
    middle = _card(31, 34, parents=[target])
    rootless = _card(32, 34)
    cycle = _card(33, 34, parents=[_card(25)])
    candidates = [_card(21), _card(22, parents=[middle]), _card(23, parents=[rootless]),
                  _card(24, parents=[_card(999, 99)], parents_count=1), _card(25, parents=[cycle]), _card(26)]
    replies = {f'/cards/{card["id"]}': card for card in [*candidates, target, middle, rootless, cycle]}
    replies['/cards/26'] = _card(26, condition=2, archived=True)
    _setup(monkeypatch, fake_api, local_store, {'/cards': candidates, **replies})
    result = _run('audit-relations', '--board', '12', '--ancestor-board', '56')['data']

    assert result['boards'] == [{'board_id': 12, 'count': 6, 'complete': True}]
    assert result['complete'] is False
    rows = {row['id']: row for row in result['cards']}
    assert rows[21]['status'] == 'missing_parent' and rows[21]['parent_ids'] == []
    assert rows[22]['status'] == 'ancestor_found' and rows[22]['path'] == [22, 31, 41]
    assert rows[23]['status'] == 'ancestor_missing' and rows[23]['rootless_ids'] == [32]
    assert rows[24]['status'] == 'incomplete' and rows[24]['reason'] == 'parent_data_incomplete'
    assert rows[24]['parent_ids'] == [] and rows[24]['parents_complete'] is False
    assert rows[25]['reason'] == 'parent_cycle'
    assert rows[26]['reason'] == 'card_no_longer_active'
    assert '/cards/999' not in [call['path'] for call in fake_api.calls()]
    assert all(call['method'] == 'GET' for call in fake_api.calls())


def test_audit_keeps_unverified_creators_out_of_actionable_results(monkeypatch, fake_api, local_store):
    candidates = [_card(21, owner_id=5), _card(22, owner_id=7), _card(23, owner_id=7)]
    replies = {f'/cards/{card["id"]}': card for card in candidates}
    _setup(monkeypatch, fake_api, local_store, {
        '/cards': candidates, **replies,
        '/spaces/8/activity': {'items': [_event(21, 7), _event(22, 5)], 'next_cursor': None},
    })
    result = _run('audit-relations', '--board', '12', '--creator', '7', '--activity-space', '8',
                  '--created-after', CUTOFF)['data']

    assert result['complete'] is False
    matching, unverified = result['cards']
    assert matching['id'] == 21 and matching['status'] == 'missing_parent' and matching['creator_match'] is True
    assert unverified['id'] == 23 and unverified['status'] == 'incomplete' and unverified['creator_match'] is None
    assert unverified['creator_reason'] == 'no_creation_event'
