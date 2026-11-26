"""New CLI workflows against the real transport with synthetic API responses."""

from copy import deepcopy
import csv
import io
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from click.testing import CliRunner
import pytest

from kaiten_cli import cli
from kaiten_cli.auth import CredentialStore
from kaiten_cli.client import KaitenClient
from kaiten_cli.editing import KaitenEditing
from kaiten_cli.mutations import KaitenMutations
from kaiten_cli.policy import KaitenError, LocalConfigStore
from kaiten_cli.read import KaitenAdapter
from kaiten_cli.resources import KaitenResources


class Response(io.BytesIO):
    status = 200


class SyntheticAPI:
    def __init__(self):
        self.users = {
            7: {'id': 7, 'full_name': 'Ирина', 'username': 'irina', 'email': 'hidden@example.invalid'},
            8: {'id': 8, 'full_name': 'Алексей Иванов', 'username': 'alex'},
        }
        self.cards = {
            card_id: {'id': card_id, 'title': 'Отчёт', 'board_id': board, 'column_id': 10, 'lane_id': 5,
                      'state': 1, 'condition': 1, 'type_id': 1, 'due_date': '2026-11-12T00:00:00Z',
                      'due_date_time_present': False, 'parents_count': 0, 'children_count': 0,
                      'properties': {'id_2': 'old', 'id_3': 'keep'}, 'checklists': []}
            for card_id, board in ((33, 12), (44, 34), (45, 12))
        }
        self.cards[33].update(members=[{'id': 7, 'user_id': 7, 'card_id': 33, 'type': 2, **self.users[7]}],
                              blocked=True, title='=unsafe, "title"')
        self.cards[44].update(members=[], state=3)
        self.tags = {}
        self.children = set()
        self.blocks = {}
        self.calls = []
        self.reject = {}
        self.lanes = [6]

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != 'GET']

    def open(self, request, **kwargs):
        assert request.get_header('Authorization') == 'Bearer synthetic-card-workflows'
        url = urlsplit(request.full_url)
        path = url.path.removeprefix('/api/latest')
        method = request.method
        body = json.loads(request.data) if request.data else None
        params = {key: value[0] for key, value in parse_qs(url.query).items()}
        self.calls.append((method, path, params, body))
        if (method, path) in self.reject:
            raise HTTPError(request.full_url, self.reject[method, path], 'not public', {}, io.BytesIO(b'PRIVATE'))
        result = self.dispatch(method, path, params, body)
        return Response(json.dumps(result, ensure_ascii=False).encode())

    def dispatch(self, method, path, params, body):
        if path == '/users':
            return list(self.users.values())
        if path == '/card-types':
            return [{'id': 2, 'name': 'Task', 'description_template': 'PRIVATE'}]
        if path == '/company/custom-properties':
            return [{'id': 2, 'name': 'URL', 'type': 'string'}]
        if path == '/cards':
            return [card for card in self.cards.values() if card['board_id'] == int(params['board_id'])
                    and card['condition'] == int(params.get('condition', 1))
                    and ('ids' not in params or card['id'] == int(params['ids']))]
        parts = path.split('/')[1:]
        if parts[0] == 'boards':
            board = int(parts[1])
            ids = self.lanes if parts[-1] == 'lanes' else [20]
            return [{'id': value, 'title': parts[-1], 'board_id': board} for value in ids]
        card = self.cards[int(parts[1])]
        if len(parts) == 2:
            if method == 'PATCH':
                body = deepcopy(body)
                card['properties'].update(body.pop('properties', {}))
                card.update(body)
            return card
        collection = parts[2]
        target = int(parts[3]) if len(parts) > 3 else None
        if collection == 'members':
            members = card.setdefault('members', [])
            if method == 'POST':
                member = {'card_id': card['id'], 'user_id': body['user_id'], 'type': 1, **self.users[body['user_id']]}
                members.append(member)
                return member
            if method == 'PATCH':
                for member in members:
                    member['type'] = 2 if member['user_id'] == target else 1
            if method == 'DELETE':
                members[:] = [member for member in members if member['user_id'] != target]
            return members if method == 'GET' else {'id': target}
        if collection == 'tags':
            if method == 'POST':
                self.tags[61] = {'id': 61, 'tag_id': 61, 'card_id': card['id'], **body}
                return self.tags[61]
            if method == 'DELETE':
                del self.tags[target]
            return list(self.tags.values()) if method == 'GET' else {'id': target}
        if collection == 'children':
            if method == 'POST':
                self.children.add(body['card_id'])
            if method == 'DELETE':
                self.children.remove(target)
            return [self.cards[child_id] for child_id in sorted(self.children)] if method == 'GET' else {}
        if collection == 'blockers':
            if method == 'POST':
                self.blocks[71] = {'id': 71, 'card_id': card['id'], 'released': False, **body}
                if 'blocker_card_id' in body:
                    self.blocks[71]['blocker_card'] = self.cards[body['blocker_card_id']]
                return self.blocks[71]
            if method == 'DELETE':
                self.blocks[target]['released'] = True
            if method == 'PATCH':
                self.blocks[target].update(body)
            offset = int(params.get('offset', 0))
            return list(self.blocks.values())[offset:offset + 100] if method == 'GET' else self.blocks[target]
        if collection == 'checklists':
            checklists = card['checklists']
            if target is None:
                checklist = {'id': 81, 'card_id': card['id'], 'items': [], **body}
                checklists.append(checklist)
                return checklist
            checklist = next(row for row in checklists if row['id'] == target)
            if len(parts) == 4:
                if method == 'PATCH':
                    checklist.update(body)
                else:
                    checklists.remove(checklist)
                return {'id': target}
            items = checklist['items']
            if method == 'POST':
                item = {'id': 82, 'checklist_id': target, 'deleted': False, 'checked': False, **body}
                items.append(item)
                return item
            item = next(row for row in items if row['id'] == int(parts[-1]))
            if method == 'PATCH':
                item.update(body)
            else:
                item['deleted'] = True
            return item
        raise AssertionError((method, path))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('KAITEN_TOKEN', raising=False)
    store = LocalConfigStore(tmp_path / 'personal/policy.json')
    store.save(profile='test', tenant='example.kaiten.ru', board_allowlist=[12, 34])
    credentials = CredentialStore(tmp_path / 'personal/profiles.json')
    credentials.save_credentials('test', 'example.kaiten.ru', 'synthetic-card-workflows')
    api = SyntheticAPI()
    for adapter in (KaitenAdapter, KaitenEditing, KaitenMutations, KaitenResources):
        monkeypatch.setattr(cli, adapter.__name__, lambda ignored, cls=adapter: cls(
            store, credentials=credentials, client_factory=lambda selected: KaitenClient(selected, open_request=api.open)))

    def invoke(*args, code=0, raw=False):
        result = CliRunner().invoke(cli.kaiten, [*args, *([] if raw else ['--json'])])
        assert result.exit_code == code, (result.output, result.exception)
        assert 'synthetic-card-workflows' not in result.output and 'hidden@example.invalid' not in result.output
        if raw:
            return result.output
        envelope = json.loads(result.output)
        return envelope['data'] if code == 0 else envelope['error']

    return api, invoke


def test_search_add_assign_and_remove_people(workspace):
    api, invoke = workspace
    assert invoke('users', 'find', 'Иванов')['users'][0]['id'] == 8
    assert invoke('users', 'find', '@IRINA')['users'][0]['id'] == 7
    assert invoke('members', 'add', '33', '8', '--authorization', 'direct')['status'] == 'applied'
    args = ('members', 'assign', '33', '@alex', '--board', '12')
    preview = invoke(*args, '--dry-run')
    assert preview['changes']['user']['id'] == 8
    assert invoke(*args, '--authorization', 'approved', '--approval', preview['approval'])['after']['8']['type'] == 2
    assert invoke('members', 'remove', '33', '8', '--authorization', 'direct')['status'] == 'applied'
    assert [entry['user_id'] for entry in invoke('members', 'list', '33')['members']] == [7]
    assert [(m, p) for m, p, _, _ in api.writes] == [
        ('POST', '/cards/33/members'), ('PATCH', '/cards/33/members/8'), ('DELETE', '/cards/33/members/8')]


def test_assignment_partial_then_safe_separate_retry(workspace):
    api, invoke = workspace
    api.reject['PATCH', '/cards/33/members/8'] = 403
    args = ('members', 'assign', '33', '8', '--authorization', 'direct')
    error = invoke(*args, code=1)
    assert error['code'] == 'mutation_partial'
    assert error['details']['confirmed_steps'] == 1 and error['details']['after']['8']['type'] == 1
    api.reject.clear()
    assert invoke(*args)['attempts'] == 1
    assert [method for method, *_ in api.writes] == ['POST', 'PATCH', 'PATCH']


def test_edit_type_property_and_tags(workspace):
    api, invoke = workspace
    assert invoke('cards', 'catalog', 'properties')['properties'] == [{'id': 2, 'name': 'URL', 'type': 'string'}]
    assert invoke('cards', 'catalog', 'types')['types'] == [{'id': 2, 'name': 'Task'}]
    args = ('cards', 'update', '33', '--type', '2', '--property', '2="https://example.invalid"', '--property', '7=null')
    preview = invoke(*args, '--dry-run')
    assert preview['before'] == {'type_id': 1, 'properties': {'id_2': 'old', 'id_7': None}}
    assert invoke(*args, '--authorization', 'approved', '--approval', preview['approval'])['status'] == 'applied'
    assert api.cards[33]['properties']['id_3'] == 'keep'
    assert any(params.get('broken_api') == 'false' for _, path, params, _ in api.calls if path == '/cards/33')
    assert invoke('tags', 'add', '33', 'Срочно', '--authorization', 'direct')['status'] == 'applied'
    assert invoke('tags', 'list', '33')['tags'][0]['tag_id'] == 61
    assert invoke('tags', 'remove', '33', '61', '--authorization', 'direct')['status'] == 'applied'
    assert invoke('cards', 'update', '33', '--property', '2=NaN', '--dry-run', code=1)['code'] == 'property_invalid'


def test_checklist_and_item_workflow(workspace):
    api, invoke = workspace
    direct = ('--board', '12', '--authorization', 'direct')
    assert invoke('checklists', 'add', '33', '--name', 'QA', *direct)['status'] == 'applied'
    assert invoke('checklists', 'update', '33', '--checklist', '81', '--name', 'Ready', *direct)['status'] == 'applied'
    assert invoke('checklists', 'item-add', '33', '--checklist', '81', '--text', 'Проверить', *direct)['status'] == 'applied'
    assert invoke('checklists', 'item-update', '33', '--checklist', '81', '--item', '82', '--checked',
                  '--responsible', '8', '--due-date', '2026-12-01', *direct)['status'] == 'applied'
    checklist = invoke('checklists', 'list', '33')['checklists'][0]
    assert checklist['name'] == 'Ready' and checklist['items'][0]['checked'] is True
    assert invoke('checklists', 'item-remove', '33', '--checklist', '81', '--item', '82', *direct)['status'] == 'applied'
    assert invoke('checklists', 'remove', '33', '--checklist', '81', *direct)['status'] == 'applied'
    assert api.cards[33]['checklists'] == []
    assert invoke('checklists', 'item-add', '33', '--checklist', '81', '--name', 'wrong', *direct,
                  code=1)['code'] == 'checklist_options_invalid'


def test_link_and_release_blockers_keep_both_cards_in_scope(workspace):
    api, invoke = workspace
    direct = ('--authorization', 'direct')
    assert invoke('relations', 'add', '33', '44', '--child-board', '99', *direct, code=1)['code'] == 'forbidden'
    assert not api.writes
    assert invoke('relations', 'add', '33', '44', '--child-board', '34', *direct)['status'] == 'applied'
    assert invoke('relations', 'remove', '33', '44', '--child-board', '34', *direct)['status'] == 'applied'
    assert invoke('blockers', 'add', '33', '--blocker', '44', '--blocker-board', '34',
                  '--reason', 'Ожидаем поставку', *direct)['status'] == 'applied'
    assert invoke('blockers', 'update', '33', '--block-id', '71', '--reason', 'Ожидаем данные',
                  *direct)['after']['71']['reason'] == 'Ожидаем данные'
    assert invoke('blockers', 'list', '33')['blockers'][0]['blocker_card_id'] == 44
    api.cards[44]['board_id'] = 99
    assert 'blocker_card_id' not in invoke('blockers', 'list', '33')['blockers'][0]
    assert invoke('blockers', 'release', '33', '--block-id', '71', *direct, code=1)['code'] == 'forbidden'
    api.cards[44]['board_id'] = 34
    assert invoke('blockers', 'release', '33', '--block-id', '71', *direct)['status'] == 'applied'


def test_block_release_finds_id_after_first_page(workspace):
    api, invoke = workspace
    api.blocks.update({i: {'id': i, 'card_id': 33, 'released': False, 'reason': 'report'} for i in range(1, 102)})
    assert invoke('blockers', 'release', '33', '--block-id', '101', '--authorization', 'direct')['status'] == 'applied'
    assert api.blocks[101]['released'] is True and api.blocks[100]['released'] is False


def test_cross_board_move_and_stale_target(workspace):
    api, invoke = workspace
    args = ('33', '--board', '12', '--to-board', '34', '--column', '20')
    preview = invoke('move', 'prepare', *args)
    assert preview['source']['board_id'] == 12 and preview['target']['board_id'] == 34
    api.lanes = [6, 9]
    assert invoke('move', 'apply', *args, '--authorization', 'перемещай', '--approval', preview['approval'],
                  code=1)['code'] == 'target_invalid'
    api.lanes = [6]
    result = invoke('move', 'apply', *args, '--authorization', 'перемещай', '--approval', preview['approval'])
    assert result['location'] == {'board_id': 34, 'column_id': 20, 'lane_id': 6}
    assert [(m, b) for m, _, _, b in api.writes] == [('PATCH', result['location'])]


def test_bulk_requires_exact_preview_and_stops_after_uncertain_result(workspace):
    api, invoke = workspace
    args = ('cards', 'bulk-update', '33', '44', '45', '--archive')
    preview = invoke(*args, '--dry-run')
    assert len(preview['cards']) == 3 and not api.writes
    assert invoke(*args, code=1)['code'] == 'authorization_required'
    assert invoke('cards', 'bulk-update', '44', '33', '45', '--archive', '--approval', preview['approval'],
                  code=1)['code'] == 'preview_changed'
    assert not api.writes
    api.reject['PATCH', '/cards/44'] = 503
    error = invoke(*args, '--approval', preview['approval'], code=1)
    assert error['code'] == 'mutation_partial'
    assert [row['status'] for row in error['details']['cards']] == ['applied', 'ambiguous', 'skipped']
    assert [path for _, path, _, _ in api.writes] == ['/cards/33', '/cards/44']
    api.reject.clear()
    edit = ('cards', 'bulk-update', '44', '45', '--title', 'Новый заголовок')
    preview = invoke(*edit, '--dry-run')
    assert [row['status'] for row in invoke(*edit, '--approval', preview['approval'])['cards']] == ['applied', 'applied']


def test_reports_and_export_use_scoped_reads(workspace):
    api, invoke = workspace
    report = invoke('cards', 'report', '--as-of', '2026-11-24')
    assert [card['id'] for card in report['overdue']] == [33, 45]
    assert [card['id'] for card in report['blocked']] == [33]
    assert report['membership_unknown'] == 1 and report['by_responsible'][0]['user_id'] == 7
    rows = list(csv.DictReader(io.StringIO(invoke('cards', 'export', '--board', '12', raw=True))))
    assert [row['id'] for row in rows] == ['33', '45']
    assert rows[0]['title'] == "'" + api.cards[33]['title']
    assert rows[0]['responsible_ids'] == '7' and not api.writes


def test_direct_transport_excludes_card_and_company_deletion(workspace):
    api, _ = workspace
    from kaiten_cli.auth import Credentials
    client = KaitenClient(Credentials('test', 'example.kaiten.ru', 'synthetic-card-workflows'),
                          open_request=api.open)
    for path in ('/cards/33', '/cards/33/comments/4', '/company/custom-properties/2'):
        with pytest.raises(KaitenError) as error:
            client.request('DELETE', path)
        assert error.value.code == 'request_invalid'
    assert not api.calls
