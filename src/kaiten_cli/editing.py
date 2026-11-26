"""Edit selected card fields once with a reviewed source and verified readback."""

from datetime import date, datetime
import json
import re

from .mutations import KaitenMutations, _approve
from .policy import KaitenError, load_policy
from .read import CARD_FIELDS, _scalar_fields


ARCHIVE_CONTEXT_FIELDS = ('state', 'column_id', 'lane_id', 'parents_count', 'children_count', 'updated')


def parse_properties(arguments):
    values = {}
    for argument in arguments:
        property_id, _, value = argument.partition('=')
        if not re.fullmatch(r'[1-9][0-9]*', property_id) or len(value) > 32768:
            raise KaitenError('property_invalid', 'Use --property ID=JSON with a positive property ID.')
        key = f'id_{property_id}'
        if key in values:
            raise KaitenError('property_invalid', 'Pass each property ID only once.')
        try:
            values[key] = json.loads(value)
            json.dumps(values[key], allow_nan=False)
        except (ValueError, RecursionError) as exc:
            raise KaitenError('property_invalid', 'Use a valid JSON value, or null to clear a property.') from exc
    return values


class KaitenEditing(KaitenMutations):
    def _detail(self, policy, card_id, board_id, *, lifecycle=False, type_edit=False, property_keys=()):
        if load_policy(self.store) != policy:
            raise KaitenError('policy_changed', 'Local profile/board selection changed; inspect again.')
        policy.assert_allowed('cards.read', board_id)
        self._verify_profile(policy.profile, policy.tenant)
        params = {'broken_api': 'false'} if property_keys else None
        data = self._request(policy.profile, 'GET', f'/cards/{card_id}', params=params)
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
        if type_edit:
            if ('type_id' not in data or (data['type_id'] is not None
                                        and (type(data['type_id']) is not int or data['type_id'] <= 0))):
                raise KaitenError('bad_json', 'Current card type could not be verified.')
            card['type_id'] = data['type_id']
        if property_keys:
            values = data.get('properties')
            if 'properties' not in data or (values is not None and not isinstance(values, dict)):
                raise KaitenError('bad_json', 'Card custom properties have an unexpected shape.')
            card['properties'] = {key: (values or {}).get(key) for key in property_keys}
        if lifecycle:
            if type(data.get('condition')) is not int or data['condition'] not in (1, 2):
                raise KaitenError('bad_json', 'The card archive state could not be verified.')
            card.update(_scalar_fields(data, ('condition', *ARCHIVE_CONTEXT_FIELDS)))
            card['has_relation_data'] = any(data.get(key) for key in ('parents', 'children', 'parents_ids', 'children_ids'))
        return card

    @staticmethod
    def _changes(title, description, due_date, clear_due_date, condition, type_id, properties):
        if type(clear_due_date) is not bool or (clear_due_date and due_date is not None):
            raise KaitenError('due_date_invalid', 'Choose either --due-date or --clear-due-date.')
        properties = parse_properties(properties)
        if condition is not None:
            if (type(condition) is not int or condition not in (1, 2) or clear_due_date or properties
                    or any(value is not None for value in (title, description, due_date, type_id))):
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
        if type_id is not None:
            if type(type_id) is not int or type_id <= 0:
                raise KaitenError('type_invalid', 'Use a positive card type ID.')
            changes['type_id'] = type_id
        if properties:
            changes['properties'] = properties
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
            raise KaitenError('update_empty', 'Choose a field to edit, --archive or --restore.')
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
                       due_date=None, clear_due_date=False, condition=None, type_id=None, properties=()):
        changes = self._changes(title, description, due_date, clear_due_date, condition, type_id, properties)
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
        card = self._detail(policy, found['id'], found['board_id'], lifecycle=condition is not None,
                            type_edit=type_id is not None, property_keys=changes.get('properties', ()))
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
        return self._apply_update(preview, allow_unlink=allow_unlink)

    def _apply_update(self, preview, *, allow_unlink):
        policy = load_policy(self.store)
        card_id, board_id = preview['card']['id'], preview['card']['board_id']
        policy.assert_allowed('cards.update', board_id)
        document = {key: value for key, value in preview.items() if key != 'approval'}
        condition = preview['changes'].get('condition')
        detail_options = dict(lifecycle=condition is not None, type_edit='type_id' in preview['changes'],
                              property_keys=preview['changes'].get('properties', ()))
        current = self._detail(policy, card_id, board_id, **detail_options)
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
            card = self._detail(policy, card_id, board_id, **detail_options)
        except KaitenError as exc:
            return self._outcome('not-applied' if rejected else 'ambiguous', error=error or exc,
                                 card_id=card_id, url=url, readback=False)
        matches = self._matches(card, preview['changes'])
        status = 'applied' if matches and not rejected else ('not-applied' if rejected and not matches else 'ambiguous')
        return self._outcome(status, error=error, card_id=card_id, url=url, card=card, readback=True)

    def bulk_update(self, references, *, dry_run=False, approval=None, allow_unlink=False, **options):
        if not 1 <= len(references) <= 100:
            raise KaitenError('batch_size', 'Select 1–100 explicit card IDs or URLs.')
        if not dry_run and not approval:
            raise KaitenError('authorization_required', 'Review --dry-run and supply its --approval for this batch.')
        policy = load_policy(self.store)
        card_ids = [self._card_reference(reference, policy.tenant) for reference in references]
        if None in card_ids or len(card_ids) != len(set(card_ids)):
            raise KaitenError('batch_cards_invalid', 'List each card once by numeric ID or tenant URL.')

        previews = [self.prepare_update(str(card_id), **options) for card_id in card_ids]
        if load_policy(self.store) != policy:
            raise KaitenError('policy_changed', 'Local board selection changed; prepare the batch again.')
        document = {'cards': previews}
        expected = self._digest(policy, 'card-bulk-update', document)
        if dry_run:
            return {'dry_run': True, **document, 'approval': expected}
        _approve(expected, approval)
        if not allow_unlink and any(item.get('archive_context', {}).get('may_remove_relations') for item in previews):
            raise KaitenError('archive_may_remove_relations',
                              'One or more cards may lose links. Explicit approval and --allow-unlink are required.', document)

        results = []
        failed = False
        for preview in previews:
            card_id = preview['card']['id']
            if failed:
                results.append({'card_id': card_id, 'status': 'skipped', 'attempts': 0})
                continue
            try:
                if load_policy(self.store) != policy:
                    raise KaitenError('policy_changed', 'Local board selection changed; inspect the remaining cards.')
                outcome = self._apply_update(preview, allow_unlink=allow_unlink)
            except KaitenError as exc:
                outcome = {'card_id': card_id, 'status': 'not-applied', 'attempts': 0, 'error_code': exc.code}
            results.append(outcome)
            failed = outcome['status'] not in {'applied', 'already-applied'}
        return {'status': 'partial' if failed else 'applied', 'cards': results, 'retry_safe': False}
