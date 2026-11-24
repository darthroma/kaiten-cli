"""Edit selected card fields once with a reviewed source and verified readback."""

from datetime import date, datetime
import re

from .mutations import KaitenMutations, _approve
from .policy import KaitenError, load_policy
from .read import CARD_FIELDS, _scalar_fields


ARCHIVE_CONTEXT_FIELDS = ('state', 'column_id', 'lane_id', 'parents_count', 'children_count', 'updated')


class KaitenEditing(KaitenMutations):
    def _detail(self, policy, card_id, board_id, *, lifecycle=False):
        if load_policy(self.store) != policy:
            raise KaitenError('policy_changed', 'Local profile/board selection changed; inspect again.')
        policy.assert_allowed('cards.read', board_id)
        self._verify_profile(policy.profile, policy.tenant)
        data = self._request(policy.profile, 'GET', f'/cards/{card_id}')
        if (not isinstance(data, dict) or type(data.get('id')) is not int or data['id'] != card_id
                or type(data.get('board_id')) is not int or data['board_id'] != board_id):
            raise KaitenError('forbidden', 'Card identity or board changed; no out-of-scope data is returned.')
        if (not isinstance(data.get('title'), str)
                or (data.get('description') is not None and not isinstance(data['description'], str))
                or (data.get('due_date') is not None and not isinstance(data['due_date'], str))
                or (data.get('due_date_time_present') is not None and type(data['due_date_time_present']) is not bool)):
            raise KaitenError('bad_json', 'Card fields have an unexpected shape.')
        if load_policy(self.store) != policy:
            raise KaitenError('policy_changed', 'Local policy changed while reading the card.')
        card = {key: data.get(key) for key in (*CARD_FIELDS.split(','), 'due_date', 'due_date_time_present')}
        if lifecycle:
            if type(data.get('condition')) is not int or data['condition'] not in (1, 2):
                raise KaitenError('bad_json', 'The card archive state could not be verified.')
            card.update(_scalar_fields(data, ('condition', *ARCHIVE_CONTEXT_FIELDS)))
            card['has_relation_data'] = any(data.get(key) for key in ('parents', 'children', 'parents_ids', 'children_ids'))
        return card

    @staticmethod
    def _changes(title, description, due_date, clear_due_date, condition):
        if condition is not None:
            if (type(condition) is not int or condition not in (1, 2) or clear_due_date
                    or any(value is not None for value in (title, description, due_date))):
                raise KaitenError('archive_options_invalid', 'Run --archive or --restore separately from other edits.')
            return {'condition': condition}
        changes = {}
        if title is not None:
            if not isinstance(title, str) or not title.strip() or '\x00' in title or len(title) > 1024:
                raise KaitenError('title_invalid', 'Provide 1–1024 characters of card title.')
            changes['title'] = title
        if description is not None:
            if not isinstance(description, str) or '\x00' in description or len(description) > 32768:
                raise KaitenError('description_invalid', 'Description must contain at most 32768 characters without NUL.')
            changes['description'] = description
        if type(clear_due_date) is not bool or (clear_due_date and due_date is not None):
            raise KaitenError('due_date_invalid', 'Choose either --due-date or --clear-due-date.')
        if due_date is not None:
            try:
                if not isinstance(due_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', due_date):
                    raise ValueError
                date.fromisoformat(due_date)
            except ValueError as exc:
                raise KaitenError('due_date_invalid', 'Use a valid deadline in YYYY-MM-DD format.') from exc
        if due_date is not None or clear_due_date:
            changes.update(due_date=due_date, due_date_time_present=False)
        if not changes:
            raise KaitenError('update_empty', 'Choose --title, --description, --due-date, --clear-due-date, --archive or --restore.')
        return changes

    @staticmethod
    def _matches(card, changes):
        for key, expected in changes.items():
            actual = card.get(key)
            if key == 'description' and expected == '' and actual is None:
                continue
            if key == 'due_date' and expected is not None:
                try:
                    if not isinstance(actual, str) or datetime.fromisoformat(actual.replace('Z', '+00:00')).date().isoformat() != expected:
                        return False
                except ValueError:
                    return False
            elif actual != expected:
                return False
        return True

    @staticmethod
    def _archive_context(card, condition):
        context = {key: card.get(key) for key in ARCHIVE_CONTEXT_FIELDS}
        known_empty = (not card['has_relation_data'] and all(type(context[key]) is int and context[key] == 0
                       for key in ('parents_count', 'children_count')))
        done = type(card['state']) is int and card['state'] == 3
        context['may_remove_relations'] = condition == 2 and card['condition'] == 1 and not done and not known_empty
        return context

    def prepare_update(self, reference, *, board_id=None, title=None, description=None,
                       due_date=None, clear_due_date=False, condition=None):
        changes = self._changes(title, description, due_date, clear_due_date, condition)
        policy = load_policy(self.store)
        policy.assert_allowed('cards.update', board_id)
        card_id = self._card_reference(reference, policy.tenant)
        if card_id is None:
            raise KaitenError('query_invalid', 'Resolve a card ID or HTTPS card URL before editing.')
        if condition is None:
            policy, found = self._card(reference, board_id, 'cards.update')
        else:
            boards = (board_id,) if board_id is not None else policy.board_allowlist
            result = self._find_card_including_archive(policy, boards, card_id)
            if not result['complete']:
                raise KaitenError('incomplete', 'Narrow the board before editing the card archive state.')
            if not result['cards']:
                raise KaitenError('not_found', 'Card not found on the requested allowed boards.')
            found = result['cards'][0]
        card = self._detail(policy, found['id'], found['board_id'], lifecycle=condition is not None)
        preview = {'card': {key: card[key] for key in ('id', 'title', 'board_id')},
                   'before': {key: card[key] for key in changes}, 'changes': changes}
        if condition is not None:
            preview['archive_context'] = self._archive_context(card, condition)
        preview['approval'] = self._digest(policy, 'card-update', preview)
        return preview

    def update_card(self, reference, *, authorization=None, approval=None, allow_unlink=False, **kwargs):
        if authorization not in {'direct', 'approved'}:
            raise KaitenError('authorization_required', 'An exact user edit request or an approved update preview is required.')
        preview = self.prepare_update(reference, **kwargs)
        if authorization == 'approved':
            _approve(preview['approval'], approval)
        policy = load_policy(self.store)
        card_id, board_id = preview['card']['id'], preview['card']['board_id']
        policy.assert_allowed('cards.update', board_id)
        document = {key: value for key, value in preview.items() if key != 'approval'}
        condition = preview['changes'].get('condition')
        current = self._detail(policy, card_id, board_id, lifecycle=condition is not None)
        if {key: current[key] for key in preview['changes']} != preview['before']:
            raise KaitenError('preview_changed', 'The fields being edited changed during inspection; prepare again.')
        if condition is not None:
            archive_context = self._archive_context(current, condition)
            if archive_context != preview['archive_context']:
                raise KaitenError('preview_changed', 'Card location or relation information changed; prepare again.')
            if archive_context['may_remove_relations'] and allow_unlink is not True:
                raise KaitenError('archive_may_remove_relations',
                                  'Archiving this card may remove its parent and child links. Run --dry-run and use --allow-unlink only with explicit approval of that consequence.',
                                  preview)
        _approve(self._digest(policy, 'card-update', document), preview['approval'])
        url = f'https://{policy.tenant}/boards/card/{card_id}'
        if self._matches(current, preview['changes']):
            return {'status': 'already-applied', 'attempts': 0, 'retry_safe': False,
                    'card_id': card_id, 'url': url, 'card': current, 'readback': True}
        _, error = self._write_once(policy, 'PATCH', f'/cards/{card_id}', preview['changes'])
        rejected = self._definite_rejection(error) or (error is not None and error.details.get('status_code') in {402, 413})
        try:
            _approve(self._digest(policy, 'card-update', document), preview['approval'])
            card = self._detail(policy, card_id, board_id, lifecycle=condition is not None)
        except KaitenError as exc:
            return self._outcome('not-applied' if rejected else 'ambiguous', error=error or exc,
                                 card_id=card_id, url=url, readback=False)
        matches = self._matches(card, preview['changes'])
        status = 'applied' if matches and not rejected else ('not-applied' if rejected and not matches else 'ambiguous')
        return self._outcome(status, error=error, card_id=card_id, url=url, card=card, readback=True)
