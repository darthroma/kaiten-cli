"""Board-scoped card reports and CSV projection for search results."""

from collections import defaultdict
import csv
from datetime import datetime
from io import StringIO


def _responsible(card):
    members = card.get('members')
    if not isinstance(members, list):
        return None
    return {member.get('user_id', member.get('id')): member for member in members
            if member.get('type') == 2 and type(member.get('user_id', member.get('id'))) is int}


def _is_overdue(card, as_of):
    deadline = card.get('due_date')
    state = card.get('state')
    if type(state) is not int or state not in (1, 2) or not isinstance(deadline, str):
        return False
    try:
        return datetime.fromisoformat(deadline).date() < as_of
    except ValueError:
        return False


def card_report(result, as_of, kind):
    groups = defaultdict(lambda: {'total': 0, 'overdue': 0, 'blocked': 0})
    overdue = []
    blocked = []
    membership_unknown = 0
    for card in result['cards']:
        late = _is_overdue(card, as_of)
        is_blocked = card.get('blocked') is True
        if late:
            overdue.append(card)
        if is_blocked:
            blocked.append(card)
        responsible = _responsible(card)
        if responsible is None:
            membership_unknown += 1
            continue
        for user_id, member in (responsible or {None: {}}).items():
            group = groups[user_id]
            group['user_id'] = user_id
            if member:
                group.update(full_name=member.get('full_name'), username=member.get('username'))
            group['total'] += 1
            group['overdue'] += late
            group['blocked'] += is_blocked

    report = {'as_of': as_of.isoformat(), 'complete': result['complete'], 'total': len(result['cards']),
              'overdue_count': len(overdue), 'blocked_count': len(blocked),
              'membership_unknown': membership_unknown}
    if kind in ('all', 'overdue'):
        report['overdue'] = overdue
    if kind in ('all', 'blocked'):
        report['blocked'] = blocked
    if kind in ('all', 'responsible'):
        report['by_responsible'] = sorted(groups.values(), key=lambda row: (row['user_id'] is None, row['user_id'] or 0))
    return report


CSV_FIELDS = ('id', 'key', 'title', 'description', 'board_id', 'column_id', 'lane_id', 'state',
              'condition', 'type_id', 'due_date', 'blocked', 'responsible_ids', 'tags')


def _csv_cell(value):
    if isinstance(value, str) and (value.startswith(('\t', '\r', '\n')) or value.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + value
    return value


def cards_csv(result):
    stream = StringIO(newline='')
    writer = csv.DictWriter(stream, CSV_FIELDS, lineterminator='\r\n')
    writer.writeheader()
    for card in result['cards']:
        row = {key: card.get(key) for key in CSV_FIELDS}
        row['responsible_ids'] = ','.join(str(user_id) for user_id in sorted(_responsible(card) or {}))
        row['tags'] = ', '.join(tag['name'] for tag in card.get('tags', []) if isinstance(tag.get('name'), str))
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return stream.getvalue()
