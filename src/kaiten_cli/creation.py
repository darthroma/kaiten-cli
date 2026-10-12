"""Create one scoped card; verify the returned identity, never guess or retry."""

from datetime import date
import re

from .mutations import KaitenMutations, _approve, _normalized
from .policy import KaitenError, load_policy
from .read import CARD_FIELDS


class KaitenCreation(KaitenMutations):
    def _responsible(self, policy, query):
        if not isinstance(query, str) or not query.strip() or '\x00' in query:
            raise KaitenError('responsible_invalid', 'Select an exact full name, @username or user ID.')
        users, seen = [], set()
        for page in range(20):
            batch = self._request(policy.profile, 'GET', '/users', params={'limit': 100, 'offset': page * 100})
            if not isinstance(batch, list):
                raise KaitenError('bad_json', 'Unexpected user directory response.')
            for user in batch:
                if (not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] <= 0
                        or user['id'] in seen or not isinstance(user.get('full_name'), str)
                        or not isinstance(user.get('username'), str)):
                    raise KaitenError('bad_json', 'Invalid or repeated user identity.')
                seen.add(user['id'])
                if user.get('activated') is not False:
                    users.append({key: user[key] for key in ('id', 'full_name', 'username')})
            if len(batch) < 100:
                break
        else:
            raise KaitenError('incomplete', 'User directory is incomplete; no card was created.')
        key = _normalized(query.removeprefix('@'))
        matches = [user for user in users if (str(user['id']) == key if key.isdecimal()
                   else key in {_normalized(user['full_name']), _normalized(user['username'])})]
        if len(matches) != 1:
            raise KaitenError('responsible_ambiguous' if matches else 'responsible_not_found',
                              'Select one exact active responsible account; no card was created.', {'candidates': matches})
        return matches[0]

    def prepare_card(self, title, *, board_id, column_id, lane_id=None, description=None,
                     due_date=None, responsible=None):
        if not isinstance(title, str) or not title.strip() or '\x00' in title or len(title) > 1024:
            raise KaitenError('title_invalid', 'Provide 1–1024 characters of card title.')
        if description is not None and (not isinstance(description, str) or '\x00' in description or len(description) > 32768):
            raise KaitenError('description_invalid', 'Description must contain at most 32768 characters without NUL.')
        if any(type(value) is not int or value <= 0 for value in (board_id, column_id)) or (
                lane_id is not None and (type(lane_id) is not int or lane_id <= 0)):
            raise KaitenError('target_invalid', 'Board, column and lane IDs must be positive integers.')
        if due_date is not None:
            try:
                if not isinstance(due_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', due_date):
                    raise ValueError
                date.fromisoformat(due_date)
            except ValueError as exc:
                raise KaitenError('due_date_invalid', 'Use a valid deadline in YYYY-MM-DD format.') from exc
        policy = load_policy(self.store)
        policy.assert_allowed('cards.create', board_id)
        self._verify_profile(policy.profile, policy.tenant)
        topology = self._topology(policy, board_id)
        column = next((item for item in topology['columns'] if item['id'] == column_id), None)
        if lane_id is None and len(topology['lanes']) == 1:
            lane_id = topology['lanes'][0]['id']
        lane = next((item for item in topology['lanes'] if item['id'] == lane_id), None)
        if column is None or lane is None:
            raise KaitenError('target_invalid', 'Choose an existing column and lane on the allowed board; specify --lane if there are several.', topology)
        payload = {'title': title, 'board_id': board_id, 'column_id': column_id, 'lane_id': lane_id}
        if description is not None:
            payload['description'] = description
        if due_date is not None:
            payload.update(due_date=due_date, due_date_time_present=False)
        account = self._responsible(policy, responsible) if responsible is not None else None
        if account is not None:
            payload['responsible_id'] = account['id']
        if load_policy(self.store) != policy:
            raise KaitenError('policy_changed', 'Local board/profile selection changed; prepare again.')
        preview = {'payload': payload, 'target': {'board_id': board_id, 'column': column, 'lane': lane},
                   'responsible': account}
        preview['approval'] = self._digest(policy, 'card-create', preview)
        return preview

    def create_card(self, title, *, authorization=None, approval=None, **kwargs):
        if authorization not in {'direct', 'approved'}:
            raise KaitenError('authorization_required', 'An exact user request or an approved creation preview is required.')
        preview = self.prepare_card(title, **kwargs)
        if authorization == 'approved':
            _approve(preview['approval'], approval)
        policy = load_policy(self.store)
        expected = preview['payload']
        policy.assert_allowed('cards.create', expected['board_id'])
        # Refresh the HTTP credentials as well as checking the token-bound preview.
        self._verify_profile(policy.profile, policy.tenant)
        _approve(self._digest(policy, 'card-create', {k: v for k, v in preview.items() if k != 'approval'}), preview['approval'])
        reply, error = self._write_once(policy, 'POST', '/cards', expected)
        rejected = self._definite_rejection(error) or (error is not None and error.details.get('status_code') in {402, 413})
        if rejected:
            return self._outcome('not-applied', error=error, readback=False)
        # Never infer a new identity by title: identical cards may already exist.
        if (not isinstance(reply, dict) or type(reply.get('id')) is not int or reply['id'] <= 0
                or type(reply.get('board_id')) is not int or reply['board_id'] != expected['board_id']):
            return self._outcome('ambiguous', error=error, readback=False)
        card_id = reply['id']
        url = f'https://{policy.tenant}/boards/card/{card_id}'
        try:
            if load_policy(self.store) != policy:
                raise KaitenError('policy_changed', 'Local policy changed after creation.')
            self._verify_profile(policy.profile, policy.tenant)
            _approve(self._digest(policy, 'card-create', {k: v for k, v in preview.items() if k != 'approval'}), preview['approval'])
            card = self._request(policy.profile, 'GET', f'/cards/{card_id}')
            if (not isinstance(card, dict) or type(card.get('id')) is not int or card['id'] != card_id
                    or type(card.get('board_id')) is not int or card['board_id'] != expected['board_id']):
                raise KaitenError('bad_json', 'Created card identity/location could not be verified.')
            verified = all(card.get(key) == expected[key] for key in ('title', 'board_id', 'column_id', 'lane_id'))
            if 'description' in expected:
                verified = verified and (card.get('description') or '') == expected['description']
            if 'due_date' in expected:
                value = card.get('due_date')
                verified = verified and isinstance(value, str) and value[:10] == expected['due_date'] and card.get('due_date_time_present') is False
            if 'responsible_id' in expected:
                members = self._request(policy.profile, 'GET', f'/cards/{card_id}/members')
                if not isinstance(members, list):
                    raise KaitenError('bad_json', 'Unexpected card members response.')
                verified = verified and any(isinstance(member, dict) and member.get('user_id') == expected['responsible_id']
                    and member.get('card_id') == card_id and member.get('type') == 2 for member in members)
            safe_card = {key: card.get(key) for key in CARD_FIELDS.split(',')}
            if 'due_date' in expected:
                safe_card['due_date'] = card.get('due_date')
            if 'responsible_id' in expected:
                safe_card['responsible_id'] = expected['responsible_id'] if verified else None
            return self._outcome('applied' if verified else 'ambiguous', error=error,
                                 card_id=card_id, url=url, card=safe_card, readback=True)
        except KaitenError as exc:
            return self._outcome('ambiguous', error=error or exc, card_id=card_id, url=url, readback=False)
