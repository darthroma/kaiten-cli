"""Card-scoped people, tags, relationships, blockers and checklists."""

from dataclasses import dataclass
from datetime import date
from typing import Callable

from .mutations import KaitenMutations, _approve, _normalized
from .policy import KaitenError, load_policy
from .read import _scalar_fields


def _positive(value):
    return type(value) is int and value > 0


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > maximum:
        raise KaitenError('text_invalid', f'Provide 1–{maximum} characters without NUL.')
    return value


def _rows(data, fields, *, id_key='id'):
    if not isinstance(data, list):
        raise KaitenError('bad_json', 'Kaiten returned an unexpected list.')
    rows = {}
    for row in data:
        if not isinstance(row, dict) or not _positive(row.get(id_key)) or row[id_key] in rows:
            raise KaitenError('bad_json', 'Kaiten returned an invalid or duplicate resource ID.')
        rows[row[id_key]] = _scalar_fields(row, (id_key, *fields))
    return rows


def _returned_id(reply):
    return reply.get('id') if isinstance(reply, dict) and _positive(reply.get('id')) else None


def _new_record(after, reply, before, fields):
    record_id = _returned_id(reply)
    return (record_id is not None and record_id not in before
            and record_id in after and all(after[record_id].get(key) == value for key, value in fields.items()))


@dataclass(frozen=True)
class Step:
    method: str
    path: str
    body: dict | None
    verified: Callable


class KaitenResources(KaitenMutations):
    def _identity(self, policy, reference, board_id):
        policy.assert_allowed('cards.resources', board_id)
        card_id = self._card_reference(reference, policy.tenant)
        if card_id is None:
            raise KaitenError('query_invalid', 'Use the card ID or HTTPS card URL.')
        boards = (board_id,) if board_id is not None else policy.board_allowlist
        result = self._find_card_including_archive(policy, boards, card_id)
        if load_policy(self.store) != policy:
            raise KaitenError('policy_changed', 'Local board selection changed; inspect again.')
        if not result['complete']:
            raise KaitenError('incomplete', 'Narrow to the card board before continuing.')
        if not result['cards']:
            raise KaitenError('not_found', 'Card not found on the requested allowed boards.')
        return {key: result['cards'][0][key] for key in ('id', 'title', 'board_id')}

    def _scope(self, policy, cards):
        primary, *related = cards
        self._verify_profile(policy.profile, policy.tenant)
        for card in related:
            self._card_detail(policy, card['id'], card['board_id'])
        return self._card_detail(policy, primary['id'], primary['board_id'])

    def _change(self, policy, cards, operation, read, build, *,
                dry_run=False, authorization=None, approval=None):
        before = read()
        changes, steps = build(before)
        preview = {'cards': cards, 'operation': operation, 'before': before, 'changes': changes}
        expected = self._digest(policy, operation, preview)
        if dry_run:
            return {'dry_run': True, **preview, 'approval': expected}
        if authorization not in {'direct', 'approved'}:
            raise KaitenError('authorization_required', 'Use an exact user request or an approved preview.')
        if authorization == 'approved':
            _approve(expected, approval)
        if not steps:
            return {'status': 'already-applied', 'attempts': 0, 'readback': True,
                    'card_id': cards[0]['id'], 'after': before}
        if read() != before:
            raise KaitenError('preview_changed', 'The resource changed during inspection; prepare again.')

        after = before
        for attempts, step in enumerate(steps, start=1):
            reply = error = None
            sent = readback = verified = False
            try:
                if load_policy(self.store) != policy:
                    raise KaitenError('policy_changed', 'Local board selection changed; inspect again.')
                _approve(self._digest(policy, operation, preview), expected)
                reply, error = self._write_once(policy, step.method, step.path, step.body)
                sent = True
                _approve(self._digest(policy, operation, preview), expected)
                after = read()
                readback = True
                verified = step.verified(after, reply)
            except KaitenError as exc:
                if not sent and attempts == 1:
                    raise
                error = error or exc
            rejected = sent and (self._definite_rejection(error)
                                 or (error is not None and error.details.get('status_code') == 402))
            if not verified or rejected:
                state = 'not-applied' if not sent or (rejected and not verified) else 'ambiguous'
                return {'status': 'partial' if attempts > 1 else state,
                        'failed_step_status': state, 'confirmed_steps': attempts - 1,
                        'attempts': attempts if sent else attempts - 1, 'retry_safe': False,
                        'readback': readback, 'error_code': error.code if error else None,
                        'card_id': cards[0]['id'], 'after': after if readback else None}
        return {'status': 'applied', 'attempts': len(steps), 'retry_safe': False,
                'card_id': cards[0]['id'], 'readback': True, 'after': after}

    def _resource(self, reference, board_id, collection):
        policy = load_policy(self.store)
        card = self._identity(policy, reference, board_id)
        return policy, card, f'/cards/{card["id"]}/{collection}'

    def _members(self, policy, card, path):
        self._scope(policy, [card])
        data = self._request(policy.profile, 'GET', path)
        rows = _rows(data, ('card_id', 'full_name', 'username', 'type'), id_key='user_id')
        for row in rows.values():
            if (row.get('card_id') != card['id'] or type(row.get('type')) is not int
                    or row['type'] not in (1, 2)):
                raise KaitenError('bad_json', 'Kaiten returned an invalid card membership.')
        return rows

    def members(self, reference, *, board_id=None, action='list', user=None, **auth):
        policy, card, path = self._resource(reference, board_id, 'members')

        def read():
            return self._members(policy, card, path)

        if action == 'list':
            return {'card': card, 'members': list(read().values())}

        if action == 'remove':
            candidates = [{'id': row['user_id'], 'full_name': row.get('full_name') or '',
                           'username': row.get('username') or ''} for row in read().values()]
        else:
            candidates, complete = self._active_users(policy)
            if not complete:
                raise KaitenError('incomplete', 'User directory is incomplete; select a user in Kaiten.')
        matches = self._matching_users(user, candidates, exact=True)
        if not matches and action == 'remove' and isinstance(user, str) and user.isascii() and user.isdecimal() and int(user) > 0:
            matches = [{'id': int(user)}]
        if len(matches) != 1:
            raise KaitenError('user_ambiguous' if matches else 'user_not_found',
                              'Select one exact user ID, full name or @username.', {'candidates': matches})
        selected = matches[0]
        user_id = selected['id']
        member_path = f'{path}/{user_id}'

        def build(before):
            steps = []
            if action == 'remove':
                if user_id in before:
                    steps.append(Step('DELETE', member_path, None, lambda after, _: user_id not in after))
            else:
                if user_id not in before:
                    steps.append(Step('POST', path, {'user_id': user_id}, lambda after, _: user_id in after))
                if action == 'assign' and before.get(user_id, {}).get('type') != 2:
                    steps.append(Step('PATCH', member_path, {'type': 2},
                                      lambda after, _: after.get(user_id, {}).get('type') == 2))
            return {'user': selected, 'role': 'responsible' if action == 'assign' else 'member'}, steps

        return self._change(policy, [card], f'members-{action}', read, build, **auth)

    def tags(self, reference, *, board_id=None, action='list', name=None, tag_id=None, **auth):
        policy, card, path = self._resource(reference, board_id, 'tags')

        def read():
            self._scope(policy, [card])
            data = self._request(policy.profile, 'GET', path)
            rows = _rows(data, ('name', 'color', 'card_id', 'tag_id'))
            if any(row.get('card_id') != card['id'] or not _positive(row.get('tag_id'))
                   or not isinstance(row.get('name'), str) for row in rows.values()):
                raise KaitenError('bad_json', 'Kaiten returned an invalid tag assignment.')
            by_tag = {row['tag_id']: row for row in rows.values()}
            if len(by_tag) != len(rows):
                raise KaitenError('bad_json', 'Kaiten returned a duplicate tag assignment.')
            return by_tag

        if action == 'list':
            return {'card': card, 'tags': list(read().values())}

        if action == 'add':
            name = _text(name, 128)

            def build(before):
                def found(after, _):
                    return any(_normalized(row['name']) == _normalized(name) for row in after.values())
                steps = [] if found(before, None) else [Step('POST', path, {'name': name}, found)]
                return {'name': name}, steps
        else:
            def build(before):
                steps = []
                if tag_id in before:
                    steps.append(Step('DELETE', f'{path}/{tag_id}', None, lambda after, _: tag_id not in after))
                return {'tag_id': tag_id}, steps

        return self._change(policy, [card], f'tags-{action}', read, build, **auth)

    def relation(self, reference, child, *, board_id=None, child_board_id=None, remove=False, **auth):
        policy, parent, path = self._resource(reference, board_id, 'children')
        child = self._identity(policy, child, child_board_id)
        if parent['id'] == child['id']:
            raise KaitenError('relation_invalid', 'Select two different cards.')
        cards = [parent, child]
        child_id = child['id']

        def read():
            self._scope(policy, cards)
            rows, complete = self._directory(policy, path, ('board_id',))
            matched = next((row for row in rows if row['id'] == child_id), None)
            if matched and matched.get('board_id') != child['board_id']:
                raise KaitenError('forbidden', 'The linked card board changed; inspect again.')
            if not matched and not complete:
                raise KaitenError('incomplete', 'Child list is incomplete; the link state is uncertain.')
            return {'linked': matched is not None}

        def build(before):
            expected = not remove
            steps = []
            if before['linked'] != expected:
                method, target, body = ('DELETE', f'{path}/{child_id}', None) if remove else (
                    'POST', path, {'card_id': child_id})
                steps.append(Step(method, target, body, lambda after, _: after['linked'] == expected))
            return {'linked': expected}, steps

        return self._change(policy, cards, 'relation-remove' if remove else 'relation-add', read, build, **auth)

    def blockers(self, reference, *, board_id=None, action='list', reason=None, blocker=None,
                 blocker_board_id=None, block_id=None, **auth):
        policy, card, path = self._resource(reference, board_id, 'blockers')
        cards = [card]
        if action in ('add', 'update'):
            if reason is None and blocker is None:
                raise KaitenError('blocker_invalid', 'Give a reason or a blocker card.')
            body = {'reason': _text(reason, 4096)} if reason is not None else {}
            if blocker is not None:
                related = self._identity(policy, blocker, blocker_board_id)
                if related['id'] == card['id']:
                    raise KaitenError('blocker_invalid', 'Use a different blocker card.')
                cards.append(related)
                body['blocker_card_id'] = related['id']
        elif blocker_board_id is not None:
            raise KaitenError('blocker_invalid', 'Use --blocker-board with a blocker card.')

        def read_blocks():
            self._scope(policy, cards)
            checked_boards = {item['id']: item['board_id'] for item in cards[1:]}
            data, complete = self._directory(policy, path)
            rows = _rows(data, ('card_id', 'reason', 'released', 'due_date'))
            for raw in data:
                row = rows[raw['id']]
                if row.get('card_id') != card['id'] or type(row.get('released')) is not bool:
                    raise KaitenError('bad_json', 'Kaiten returned an invalid block record.')
                related_id = raw.get('blocker_card_id')
                if related_id is None:
                    continue
                related = raw.get('blocker_card')
                if (isinstance(related, dict) and _positive(related_id) and related.get('id') == related_id
                        and type(related.get('board_id')) is int and related['board_id'] in policy.board_allowlist
                        and checked_boards.get(related_id, related['board_id']) == related['board_id']):
                    row['blocker_card_id'] = related_id
                    row['blocker_board_id'] = related['board_id']
                elif related is None and _positive(related_id) and related_id in checked_boards:
                    row['blocker_card_id'] = related_id
                    row['blocker_board_id'] = checked_boards[related_id]
                else:
                    row['related_card_hidden'] = True
                    row.pop('reason', None)
            return rows, complete

        if action == 'list':
            rows, complete = read_blocks()
            return {'card': card, 'blockers': list(rows.values()), 'complete': complete}

        def read():
            rows, complete = read_blocks()
            if not complete:
                raise KaitenError('incomplete', 'Block list is incomplete; inspect in Kaiten before changing it.')
            return rows

        if action in ('release', 'update'):
            existing = read().get(block_id)
            if existing and not existing['released']:
                if existing.get('related_card_hidden'):
                    raise KaitenError('forbidden', 'The blocker card is outside the verified board scope.')
                if 'blocker_card_id' in existing and all(
                        related['id'] != existing['blocker_card_id'] for related in cards[1:]):
                    cards.append(self._identity(policy, str(existing['blocker_card_id']), existing['blocker_board_id']))

        def build(before):
            if action == 'add':
                expected = {'released': False, **body}
                return body, [Step('POST', path, body, lambda after, reply: _new_record(after, reply, before, expected))]
            target = before.get(block_id)
            steps = []
            if target and not target['released']:
                if target.get('related_card_hidden') or (target.get('blocker_card_id') is not None
                        and all(related['id'] != target['blocker_card_id'] for related in cards[1:])):
                    raise KaitenError('forbidden', 'The blocker card could not be verified within board scope.')
            if action == 'update':
                if target is None or target['released']:
                    raise KaitenError('not_found', 'Active block not found on this card.')

                def matches(after, _):
                    current = after.get(block_id, {})
                    return current.get('released') is False and all(current.get(key) == value for key, value in body.items())

                if not matches(before, None):
                    steps.append(Step('PATCH', f'{path}/{block_id}', body, matches))
                return {'block_id': block_id, **body}, steps
            if target and not target['released']:
                steps.append(Step('DELETE', f'{path}/{block_id}', None,
                                  lambda after, _: after.get(block_id, {}).get('released', True)))
            return {'block_id': block_id, 'released': True}, steps

        return self._change(policy, cards, f'blockers-{action}', read, build, **auth)

    def checklists(self, reference, *, board_id=None, action='list', checklist_id=None, item_id=None,
                   name=None, text=None, checked=None, due_date=None, clear_due_date=False,
                   responsible_id=None, clear_responsible=False, **auth):
        policy, card, path = self._resource(reference, board_id, 'checklists')

        def read():
            detail = self._scope(policy, [card])
            rows = _rows(detail.get('checklists'), ('name', 'card_id', 'sort_order'))
            for raw in detail['checklists']:
                row = rows[raw['id']]
                if row.get('card_id') != card['id'] or not isinstance(row.get('name'), str):
                    raise KaitenError('bad_json', 'Kaiten returned an invalid card checklist.')
                items = _rows(raw.get('items') if raw.get('items') is not None else [],
                              ('text', 'sort_order', 'checked', 'due_date', 'responsible_id', 'deleted', 'checklist_id'))
                if any(item.get('checklist_id', raw['id']) != raw['id'] for item in items.values()):
                    raise KaitenError('bad_json', 'Kaiten returned an item from another checklist.')
                row['items'] = {key: item for key, item in items.items() if item.get('deleted') is not True}
            return rows

        if action == 'list':
            result = read()
            return {'card': card, 'checklists': [{**row, 'items': list(row['items'].values())}
                                                for row in result.values()]}

        payload = {}
        if name is not None:
            payload['name'] = _text(name, 1024)
        if text is not None:
            payload['text'] = _text(text, 4096)
        if checked is not None:
            payload['checked'] = checked
        if due_date is not None and clear_due_date or responsible_id is not None and clear_responsible:
            raise KaitenError('checklist_options_invalid', 'Choose a value or its clear flag.')
        if due_date is not None or clear_due_date:
            payload['due_date'] = due_date
        if responsible_id is not None or clear_responsible:
            payload['responsible_id'] = responsible_id
        if not payload and not action.endswith('remove'):
            raise KaitenError('checklist_options_invalid', 'Provide at least one field to change.')

        def matches(row):
            for key, expected in payload.items():
                value = row.get(key)
                if key == 'due_date' and expected is not None:
                    try:
                        value = date.fromisoformat(value[:10]).isoformat()
                    except (TypeError, ValueError):
                        return False
                if value != expected:
                    return False
            return True

        def build(before):
            if action == 'add':
                return payload, [Step('POST', path, payload, lambda after, reply: _new_record(after, reply, before, payload))]
            checklist_path = f'{path}/{checklist_id}'
            target = before.get(checklist_id)
            if action == 'remove':
                steps = [] if target is None else [Step('DELETE', checklist_path, None, lambda after, _: checklist_id not in after)]
                return {'checklist_id': checklist_id, 'remove': True}, steps
            if target is None:
                raise KaitenError('not_found', 'Checklist not found on this card.')
            if action == 'update':
                steps = [] if matches(target) else [
                    Step('PATCH', checklist_path, payload, lambda after, _: checklist_id in after and matches(after[checklist_id]))]
                return {'checklist_id': checklist_id, **payload}, steps

            def items(rows):
                if checklist_id not in rows:
                    raise KaitenError('not_found', 'Checklist disappeared during readback.')
                return rows[checklist_id]['items']

            item_path = f'{checklist_path}/items'
            current = items(before)
            if action == 'item-add':
                def added(after, reply):
                    new_id = _returned_id(reply)
                    after = items(after)
                    return new_id is not None and new_id not in current and new_id in after and matches(after[new_id])
                return {'checklist_id': checklist_id, **payload}, [Step('POST', item_path, payload, added)]
            item_path = f'{item_path}/{item_id}'
            if action == 'item-remove':
                steps = [] if item_id not in current else [
                    Step('DELETE', item_path, None, lambda after, _: item_id not in items(after))]
                return {'checklist_id': checklist_id, 'item_id': item_id, 'remove': True}, steps
            if item_id not in current:
                raise KaitenError('not_found', 'Checklist item not found on this card.')
            steps = [] if matches(current[item_id]) else [
                Step('PATCH', item_path, payload, lambda after, _: item_id in items(after) and matches(items(after)[item_id]))]
            return {'checklist_id': checklist_id, 'item_id': item_id, **payload}, steps

        return self._change(policy, [card], f'checklists-{action}', read, build, **auth)

    def catalog(self, resource):
        policy = load_policy(self.store)
        policy.assert_allowed('cards.read')
        self._verify_profile(policy.profile, policy.tenant)
        path, fields = {
            'types': ('/card-types', ('name', 'archived', 'letter')),
            'properties': ('/company/custom-properties', ('name', 'type', 'condition', 'multi_select')),
        }[resource]
        data, complete = self._directory(policy, path, fields)
        if load_policy(self.store) != policy:
            raise KaitenError('policy_changed', 'Local profile changed while reading the catalog.')
        return {resource: data, 'complete': complete}
