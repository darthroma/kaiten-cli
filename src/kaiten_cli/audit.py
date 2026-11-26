"""Read-only audits of parent relationships on allowed Kaiten boards."""

from functools import lru_cache

from .policy import KaitenError, load_policy
from .read import KaitenAdapter, _timestamp


class KaitenAudit(KaitenAdapter):
    def audit_relations(self, *, board_ids, ancestor_boards=(), created_after=None,
                        creator_id=None, activity_spaces=(), with_links=False):
        policy = load_policy(self.store)
        boards = self._allowed_boards(policy, board_ids)
        for board in ancestor_boards:
            policy.assert_allowed("cards.read", board)
        if creator_id is not None and not activity_spaces:
            raise KaitenError("query_invalid", "Pass --activity-space to verify the creator.")
        created_after = self._created_filter(created_after)
        filters = {"condition": 1}
        if created_after is not None:
            filters["created_after"] = created_after.isoformat()
        selection = self._compact_cards(policy, boards, filters, created_after=created_after)
        target_boards = set(ancestor_boards)

        @lru_cache(maxsize=256)
        def read_card(card_id, board_id):
            detail = self._card_detail(policy, card_id, board_id)
            parents, complete = self._related_cards(policy, detail, "parents", False)
            fields = ("id", "board_id", "condition", "archived", "created")
            return {key: detail.get(key) for key in fields}, parents, complete

        rows = []
        for board in selection["boards"]:
            for card_id in board.pop("card_ids"):
                row = self._audit_card(policy, read_card, card_id, board["board_id"], target_boards,
                                       created_after, creator_id, activity_spaces)
                if row is not None:
                    rows.append(row)
        if with_links:
            # Cards that failed readback have no verified current location.
            placements = self._board_spaces(policy, {row["board_id"] for row in rows if "created" in row})
            for row in rows:
                if "created" in row:
                    row.update(self._card_link_fields(policy, row, placements))
                else:
                    row.update(url=None, space_id=None, link_reason="location_unverified")
        self._check_policy(policy)
        complete = selection["complete"] and all(row["complete"] for row in rows)
        return {"boards": selection["boards"], "cards": rows, "complete": complete}

    def _audit_card(self, policy, read_card, card_id, board_id, ancestor_boards,
                    created_after, creator_id, activity_spaces):
        row = {"id": card_id, "board_id": board_id, "status": "incomplete", "complete": False}
        try:
            card, parents, parents_complete = read_card(card_id, board_id)
        except KaitenError as exc:
            if exc.code == "policy_changed":
                raise
            return {**row, "reason": exc.code}

        created = _timestamp(card["created"])
        if not (type(card["condition"]) is int and card["condition"] == 1) or card["archived"] is True:
            return {**row, "reason": "card_no_longer_active"}
        if created_after is not None and (created is None or created <= created_after):
            return {**row, "reason": "creation_time_changed_or_missing"}
        row["created"] = card["created"]
        row.update(parent_ids=[parent["id"] for parent in parents], parents_complete=parents_complete)
        if activity_spaces:
            row.update(self._card_creator(policy, card, activity_spaces))
            if creator_id is not None:
                if row["creator_id"] is not None and row["creator_id"] != creator_id:
                    return None
                row["creator_match"] = True if row["creator_id"] == creator_id else None
                if row["creator_match"] is None:
                    return {**row, "reason": "creator_unverified"}

        if not parents_complete:
            return {**row, "reason": "parent_data_incomplete"}
        if not parents:
            return {**row, "status": "missing_parent", "complete": True}
        if not ancestor_boards:
            return {**row, "status": "has_parent", "complete": True}

        path, rootless_ids, reason = _find_ancestor(read_card, card_id, parents, ancestor_boards)
        if path:
            return {**row, "status": "ancestor_found", "complete": True, "path": path}
        if reason:
            return {**row, "reason": reason}
        return {**row, "status": "ancestor_missing", "complete": True, "rootless_ids": sorted(rootless_ids)}


def _find_ancestor(read_card, card_id, parents, target_boards):
    visited = set()
    rootless_ids = set()
    reason = None

    def walk(parent, path):
        nonlocal reason
        parent_id, board_id = parent["id"], parent["board_id"]
        if parent_id in path:
            reason = reason or "parent_cycle"
            return None
        if (parent_id, board_id) in visited:
            return None
        if len(visited) >= 100:
            reason = reason or "parent_traversal_limit"
            return None
        visited.add((parent_id, board_id))
        try:
            _, ancestors, complete = read_card(parent_id, board_id)
        except KaitenError as exc:
            if exc.code == "policy_changed":
                raise
            reason = reason or exc.code
            return None

        path = [*path, parent_id]
        if board_id in target_boards:
            return path
        if not complete:
            reason = reason or "parent_data_incomplete"
        elif not ancestors:
            rootless_ids.add(parent_id)
        for ancestor in ancestors:
            match = walk(ancestor, path)
            if match:
                return match
        return None

    for parent in parents:
        match = walk(parent, [card_id])
        if match:
            return match, rootless_ids, reason
    return None, rootless_ids, reason
