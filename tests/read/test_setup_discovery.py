"""Setup metadata discovery remains explicit and cannot save/expand board scope."""

import threading
import time
import pytest

from kaiten_cli.read import KaitenAdapter
from kaiten_cli.policy import KaitenError


@pytest.mark.parametrize('kwargs,space_ids,board_spaces', [({}, [2, 3], [2, 3]),
    ({'spaces_only': True}, [2, 3], []), ({'space_ids': (3,)}, [3], [3]),
    ({'space_ids': (3, 3, 2), 'workers': 1}, [2, 3], [2, 3])])
def test_discovery_is_minimal_scoped_and_does_not_save(kwargs, space_ids, board_spaces, fake_api, local_store):
    local_store.path.unlink()
    backend = fake_api({'/spaces': [{'id': 2, 'title': 'First', 'description': 'omit'}, {'id': 3, 'title': 'Second'}],
        '/spaces/2/boards': [{'id': 12, 'title': 'Work', 'cards': [{'id': 777}]}],
        '/spaces/3/boards': [{'id': 12, 'title': 'Work', 'cards': [{'id': 777}]}]})
    result = KaitenAdapter(local_store, **backend).discover_boards(profile='test-profile', tenant='example.kaiten.ru', **kwargs)
    assert [space['id'] for space in result['spaces']] == space_ids
    assert [board['space_id'] for board in result['boards']] == board_spaces
    assert all(set(board) == {'id', 'title', 'space_id'} for board in result['boards'])
    assert result['saved'] is False and not local_store.path.exists()
    assert len(fake_api.calls()) == 1 + len(board_spaces)
    assert all(call['method'] == 'GET' for call in fake_api.calls())


@pytest.mark.parametrize('kwargs,code', [({'workers': 0}, 'workers_invalid'), ({'workers': 5}, 'workers_invalid'),
    ({'workers': True}, 'workers_invalid'), ({'space_ids': (0,)}, 'space_invalid'), ({'space_ids': (True,)}, 'space_invalid')])
def test_bad_options_fail_before_api(kwargs, code, fake_api, local_store):
    with pytest.raises(KaitenError) as failure:
        KaitenAdapter(local_store, **fake_api({})).discover_boards(profile='test-profile', tenant='example.kaiten.ru', **kwargs)
    assert failure.value.code == code and fake_api.calls() == []


def test_unknown_space_cannot_trigger_board_reads(fake_api, local_store):
    with pytest.raises(KaitenError) as failure:
        KaitenAdapter(local_store, **fake_api({'/spaces': [{'id': 2, 'title': 'First'}]})).discover_boards(
            profile='test-profile', tenant='example.kaiten.ru', space_ids=(3,))
    assert failure.value.code == 'space_not_found' and len(fake_api.calls()) == 1


def test_board_discovery_is_bounded_concurrent_and_preserves_order(fake_api, local_store):
    class ConcurrentAPI:
        def __init__(self):
            self.lock = threading.Lock()
            self.barrier = threading.Barrier(4)
            self.active = self.maximum = 0
            self.finished = []

        def request(self, method, path, **kwargs):
            assert method == 'GET'
            if path == '/spaces':
                return [{'id': i, 'title': str(i)} for i in range(1, 9)]
            space = int(path.split('/')[2])
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            if space <= 4:
                self.barrier.wait(timeout=5)
            if space == 1:
                time.sleep(0.05)
            with self.lock:
                self.active -= 1
                self.finished.append(space)
            return [{'id': 12, 'title': 'Shared'}]
    api = ConcurrentAPI()
    result = KaitenAdapter(local_store, credentials=fake_api.credentials, client_factory=lambda credentials: api).discover_boards(
        profile='test-profile', tenant='example.kaiten.ru')
    assert api.maximum == 4 and api.finished[0] != 1
    assert [board['space_id'] for board in result['boards']] == list(range(1, 9)) and not result['saved']


def test_discovery_failure_is_not_partial_success(fake_api, local_store):
    before = local_store.path.read_bytes()
    backend = fake_api({'/spaces': [{'id': 2, 'title': 'First'}, {'id': 3, 'title': 'Second'}],
                        '/spaces/2/boards': {'status': 503}, '/spaces/3/boards': []})
    with pytest.raises(KaitenError) as failure:
        KaitenAdapter(local_store, **backend).discover_boards(profile='test-profile', tenant='example.kaiten.ru')
    assert failure.value.code == 'network' and local_store.path.read_bytes() == before


def test_spaces_are_paginated_and_selected_space_on_later_page_is_found(fake_api, local_store):
    backend = fake_api({('GET', '/spaces', '0'): [{'id': i, 'title': str(i)} for i in range(1, 101)],
        ('GET', '/spaces', '100'): [{'id': 101, 'title': 'Last'}], '/spaces/101/boards': [{'id': 12, 'title': 'Selected'}]})
    result = KaitenAdapter(local_store, **backend).discover_boards(profile='test-profile', tenant='example.kaiten.ru', space_ids=(101,))
    assert result['boards'] == [{'id': 12, 'title': 'Selected', 'space_id': 101}]
    assert len(fake_api.calls()) == 3


def test_repeated_space_page_fails_instead_of_losing_or_inventing_scope(fake_api, local_store):
    backend = fake_api({'/spaces': [{'id': i, 'title': str(i)} for i in range(1, 101)]})
    with pytest.raises(KaitenError) as failure:
        KaitenAdapter(local_store, **backend).discover_boards(profile='test-profile', tenant='example.kaiten.ru')
    assert failure.value.code == 'bad_json' and len(fake_api.calls()) == 2
