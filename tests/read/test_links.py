import json

from click.testing import CliRunner

from kaiten_cli import cli
from kaiten_cli.audit import KaitenAudit


CARD = {'id': 33, 'board_id': 12, 'title': 'Task', 'condition': 1, 'parents': [], 'children': []}


def _space(space_id, *boards):
    return {'id': space_id, 'title': 'Space', 'boards': list(boards)}


def _setup(monkeypatch, fake_api, local_store, responses):
    local_store.save(profile='test-profile', tenant='example.kaiten.ru', board_allowlist=[12, 34])
    adapter = KaitenAudit(local_store, **fake_api(responses))
    monkeypatch.setattr(cli, 'KaitenAdapter', lambda store: adapter)
    monkeypatch.setattr(cli, 'KaitenAudit', lambda store: adapter)


def _run(*args, exit_code=0):
    result = CliRunner().invoke(cli.kaiten, [*args, '--json'])
    assert result.exit_code == exit_code, (result.output, result.exception)
    return json.loads(result.output)


def test_card_link_uses_current_board_and_primary_space_on_next_page(monkeypatch, fake_api, local_store):
    first_page = [_space(2, {'id': 12, 'primary_path': False})]
    first_page.extend(_space(i) for i in range(10, 109))
    _setup(monkeypatch, fake_api, local_store, {
        '/cards': [CARD], '/cards/33': CARD, '/spaces': first_page,
        ('GET', '/spaces', '100'): [_space(3, {'id': 12, 'space_id': 3, 'primary_path': True})],
    })
    data = _run('links', 'card', 'https://example.kaiten.ru/space/2/boards/card/33', '--board', '12')['data']
    assert data == {'card_id': 33, 'board_id': 12, 'title': 'Task', 'space_id': 3,
                    'url': 'https://example.kaiten.ru/space/3/boards/card/33'}
    assert [call['params']['offset'] for call in fake_api.calls() if call['path'] == '/spaces'] == [['0'], ['100']]


def test_ambiguous_space_requires_a_verified_selection(monkeypatch, fake_api, local_store):
    _setup(monkeypatch, fake_api, local_store, {
        '/cards': [CARD], '/cards/33': CARD,
        '/spaces': [_space(2, {'id': 12}), _space(3, {'id': 12})],
        '/spaces/3/boards/12': {'id': 12, 'space_id': 3},
        '/spaces/12/boards/12': {'status': 404},
    })
    error = _run('links', 'card', '33', '--board', '12', exit_code=1)['error']
    assert error['code'] == 'space_ambiguous' and error['details']['space_ids'] == [2, 3]
    assert error['details']['url'] is None
    data = _run('links', 'card', '33', '--board', '12', '--space', '3')['data']
    assert data['url'] == 'https://example.kaiten.ru/space/3/boards/card/33'
    assert _run('links', 'card', '33', '--board', '12', '--space', '12', exit_code=1)['data'] is None


def test_links_reject_out_of_scope_card_and_verify_spaces(monkeypatch, fake_api, local_store):
    _setup(monkeypatch, fake_api, local_store, {
        '/spaces/3': {'id': 3, 'title': 'Space', 'entity_type': 'space'},
        '/spaces/9': {'id': 9, 'title': 'Folder', 'entity_type': 'folder'},
    })
    assert _run('links', 'card', '33', '--board', '99', exit_code=1)['error']['code'] == 'forbidden'
    assert _run('links', 'card', 'https://other.kaiten.ru/boards/card/33', exit_code=1)['error']['code'] == 'forbidden'
    assert fake_api.calls() == []
    assert _run('links', 'space', '3')['data'] == {
        'space_id': 3, 'title': 'Space', 'url': 'https://example.kaiten.ru/space/3/boards',
    }
    assert _run('links', 'space', '9', exit_code=1)['error']['code'] == 'space_not_found'


def test_view_uses_each_related_cards_own_space_and_audit_includes_links(monkeypatch, fake_api, local_store):
    parent = {'id': 45, 'board_id': 34, 'title': 'Parent', 'condition': 2, 'archived': True}
    detail = {**CARD, 'parents': [parent]}
    _setup(monkeypatch, fake_api, local_store, {
        '/cards': [CARD], '/cards/33': detail,
        '/spaces': [_space(2, {'id': 12}), _space(3, {'id': 34})],
    })
    view = _run('cards', 'view', '33', '--board', '12', '--with-links')['data']['cards'][0]
    assert view['url'] == 'https://example.kaiten.ru/space/2/boards/card/33'
    assert view['parents'][0]['url'] == 'https://example.kaiten.ru/space/3/boards/card/45'
    report = _run('cards', 'audit-relations', '--board', '12', '--with-links')['data']
    assert report['cards'][0]['url'] == view['url'] and report['complete'] is True
    assert [call['path'] for call in fake_api.calls()].count('/spaces') == 2
