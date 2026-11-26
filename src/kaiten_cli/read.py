"""Direct official API client with board-scoped reads."""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .auth import CredentialStore
from .client import KaitenClient
from .policy import KaitenError, LocalConfigStore, load_policy, normalize_profile, normalize_tenant


CARD_FIELDS = "id,title,board_id,column_id,lane_id,state,description"
CARD_LINK_FIELDS = ("id", "title", "board_id", "archived", "condition")


CARD_METADATA_FIELDS = (
    "key archived condition owner_id type_id created updated due_date due_date_time_present "
    "planned_start planned_end completed_at completed_on_time last_moved_at "
    "asap blocked size_text estimate_workload time_spent_sum comments_total goals_total goals_done"
).split()
CARD_METADATA_OBJECTS = {
    "owner": ("id", "full_name", "username"),
    "type": ("id", "name"),
    "board": ("id", "title"),
    "column": ("id", "title"),
    "lane": ("id", "title"),
}
CARD_METADATA_LISTS = {
    "members": ("id", "user_id", "full_name", "username", "type"),
    "tags": ("id", "tag_id", "name", "color"),
}


LOCATION_FIELDS = ("board_id", "column_id", "subcolumn_id", "lane_id")


def _location_event(event, card_id):
    if (not isinstance(event, dict) or type(event.get("card_id")) is not int
            or event["card_id"] != card_id or type(event.get("board_id")) is not int
            or type(event.get("condition")) is not int or event["condition"] not in (1, 2, 3)
            or type(event.get("id")) not in (int, str) or not isinstance(event.get("changed"), str)):
        raise KaitenError("bad_json", "Kaiten API returned an invalid card history event.")
    if any(event.get(key) is not None and (type(event[key]) is not int or event[key] <= 0)
           for key in LOCATION_FIELDS) or event["board_id"] <= 0:
        raise KaitenError("bad_json", "Kaiten API returned an invalid location in card history.")
    try:
        changed = datetime.fromisoformat(event["changed"])
        event_id = int(event["id"])
        if changed.tzinfo is None or event_id <= 0:
            raise ValueError
    except ValueError as exc:
        raise KaitenError("bad_json", "Kaiten API returned an invalid history event ID or timestamp.") from exc
    fields = ("id", "card_id", *LOCATION_FIELDS, "condition", "changed")
    return changed, event_id, {key: event.get(key) for key in fields}


def _scalar_fields(data, fields):
    return {key: data[key] for key in fields
            if key in data and type(data[key]) in (str, int, float, bool, type(None))}


def _card_metadata(card):
    metadata = _scalar_fields(card, CARD_METADATA_FIELDS)
    for key, fields in CARD_METADATA_OBJECTS.items():
        value = card.get(key)
        if isinstance(value, dict):
            metadata[key] = _scalar_fields(value, fields)
    for key, fields in CARD_METADATA_LISTS.items():
        value = card.get(key)
        if isinstance(value, list):
            metadata[key] = [_scalar_fields(item, fields) for item in value
                             if isinstance(item, dict) and type(item.get("id")) is int]
    return metadata


def _timestamp(value):
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is not None:
            return timestamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _space_url(tenant, space_id):
    return f"https://{tenant}/space/{space_id}/boards"


class KaitenAdapter:
    def __init__(self, store: LocalConfigStore | None = None, *, credentials=None, client_factory=None):
        self.store = store or LocalConfigStore()
        self.credentials = credentials or CredentialStore(tracked_root=self.store.tracked_root)
        self.client_factory = client_factory or KaitenClient
        self._clients = {}

    def discover_boards(self, *, profile: str, tenant: str, space_ids=(), spaces_only=False, workers=4):
        """Explicit D02 setup exception: metadata only; never updates the allowlist."""
        if type(workers) is not int or not 1 <= workers <= 4:
            raise KaitenError("workers_invalid", "Use between one and four metadata workers.")
        if not isinstance(space_ids, (tuple, list)) or any(type(space) is not int or space <= 0 for space in space_ids):
            raise KaitenError("space_invalid", "Space IDs must be positive integers.")
        profile, tenant = normalize_profile(profile), normalize_tenant(tenant)
        self._verify_profile(profile, tenant)
        spaces = [space for _, items in self._space_pages(profile) for space in items]
        if space_ids:
            selected = set(space_ids)
            if not selected.issubset({space["id"] for space in spaces}):
                raise KaitenError("space_not_found", "A selected space was not returned by the metadata list.")
            spaces = [space for space in spaces if space["id"] in selected]
        if spaces_only or not spaces:
            return {"spaces": spaces, "boards": [], "saved": False}

        def fetch(space):
            data = self._request(profile, "GET", f"/spaces/{space['id']}/boards")
            return [{**board, "space_id": space["id"]} for board in self._metadata(data)]

        # Bound independent HTTP reads and collect in source order.
        boards = []
        pool = ThreadPoolExecutor(max_workers=min(workers, len(spaces)))
        try:
            futures = [pool.submit(fetch, space) for space in spaces]
            for future in futures:
                boards.extend(future.result())
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return {"spaces": spaces, "boards": boards, "saved": False}

    def _space_pages(self, profile):
        seen = set()
        for page in range(20):
            batch = self._request(profile, "GET", "/spaces", params={"limit": 100, "offset": page * 100})
            items = self._metadata(batch)
            ids = {space["id"] for space in items}
            if len(ids) != len(items) or ids & seen:
                raise KaitenError("bad_json", "Space pagination returned a repeated identity.")
            seen.update(ids)
            yield batch, items
            if len(batch) < 100:
                return
        raise KaitenError("incomplete", "Space catalog exceeded 2000 items.")

    @staticmethod
    def _metadata(data):
        if not isinstance(data, list) or any(not isinstance(item, dict) or type(item.get("id")) is not int
                or item["id"] <= 0 or not isinstance(item.get("title"), str) for item in data):
            raise KaitenError("bad_json", "Kaiten API metadata list has an unexpected shape.")
        return [{"id": item["id"], "title": item["title"]} for item in data]

    def _topology(self, policy, board_id):
        topology = {}
        for resource in ("columns", "lanes"):
            data = self._request(policy.profile, "GET", f"/boards/{board_id}/{resource}")
            items = self._metadata(data)
            if any(item.get("board_id", board_id) != board_id for item in data):
                raise KaitenError("forbidden", "Kaiten API returned topology from a different board.")
            topology[resource] = items
        return topology

    def _allowed_boards(self, policy, board_ids):
        boards = tuple(dict.fromkeys(board_ids)) or policy.board_allowlist
        policy.assert_allowed("cards.read")
        for board in boards:
            policy.assert_allowed("cards.read", board)
        return boards

    def space_link(self, space_id):
        policy = load_policy(self.store)
        self._verify_profile(policy.profile, policy.tenant)
        space = self._request(policy.profile, "GET", f"/spaces/{space_id}")
        if (not isinstance(space, dict) or type(space.get("id")) is not int or space["id"] != space_id
                or not isinstance(space.get("title"), str) or space.get("entity_type", "space") != "space"):
            raise KaitenError("space_not_found", "Kaiten did not confirm the requested space.")
        self._check_policy(policy)
        return {"space_id": space_id, "title": space["title"], "url": _space_url(policy.tenant, space_id)}

    def card_link(self, reference, *, board_id=None, space_id=None):
        policy = load_policy(self.store)
        boards = self._allowed_boards(policy, () if board_id is None else (board_id,))
        card_id = self._card_reference(reference, policy.tenant)
        if card_id is None:
            raise KaitenError("query_invalid", "Use a numeric card ID or HTTPS card URL.")
        result = self._find_card_including_archive(policy, boards, card_id)
        if not result["cards"]:
            code = "not_found" if result["complete"] else "incomplete"
            raise KaitenError(code, "Could not find the card on the selected allowed boards.")
        card = result["cards"][0]
        detail = self._card_detail(policy, card_id, card["board_id"])
        if space_id is None:
            placements = self._board_spaces(policy, {card["board_id"]})
            link = self._card_link_fields(policy, detail, placements)
        else:
            board = self._request(policy.profile, "GET", f"/spaces/{space_id}/boards/{card['board_id']}")
            if (not isinstance(board, dict) or type(board.get("id")) is not int or board["id"] != card["board_id"]
                    or type(board.get("space_id")) is not int or board["space_id"] != space_id):
                raise KaitenError("space_not_found", "Kaiten did not confirm the board in that space.")
            link = self._card_link_fields(policy, detail, {card["board_id"]: {space_id: False}})
        self._check_policy(policy)
        if link["url"] is None:
            raise KaitenError(link["link_reason"], "Could not select a space for the card. Specify --space if known.", link)
        return {"card_id": card_id, "board_id": detail["board_id"], "title": detail["title"], **link}

    def _board_spaces(self, policy, boards):
        placements = {board: {} for board in boards}
        for board in placements:
            policy.assert_allowed("cards.read", board)
        if not placements:
            return placements
        for batch, _ in self._space_pages(policy.profile):
            self._check_policy(policy)
            for space in batch:
                if space.get("entity_type", "space") != "space":
                    continue
                space_boards = space.get("boards")
                if space_boards is None:
                    continue
                if not isinstance(space_boards, list) or any(not isinstance(board, dict) for board in space_boards):
                    raise KaitenError("bad_json", "Kaiten returned invalid space boards.")
                for board in space_boards:
                    board_id = board.get("id")
                    if (type(board_id) is int and board_id in placements
                            and board.get("space_id", space["id"]) == space["id"]):
                        placements[board_id][space["id"]] = board.get("primary_path") is True
        return placements

    @staticmethod
    def _card_link_fields(policy, card, placements):
        spaces = placements[card["board_id"]]
        primary = [space_id for space_id, is_primary in spaces.items() if is_primary]
        if len(primary) == 1 or len(spaces) == 1:
            space_id = primary[0] if primary else next(iter(spaces))
            return {"space_id": space_id, "url": f"{_space_url(policy.tenant, space_id)}/card/{card['id']}"}
        reason = "space_ambiguous" if spaces else "space_not_found"
        return {"url": None, "space_id": None, "link_reason": reason, "space_ids": sorted(spaces)}

    def find_cards(self, query: str | None, *, board_id: int | None = None, board_ids=(), include_metadata=False,
                   responsible_id=None, member_id=None, tag=None, column_id=None, lane_id=None,
                   condition=None, due_before=None, due_after=None, created_after=None, compact=False):
        policy = load_policy(self.store)
        boards = self._allowed_boards(policy, (board_id,) if board_id is not None else board_ids)
        created_after = self._created_filter(created_after)
        filters = {key: value for key, value in {
            "responsible_id": responsible_id, "member_ids": member_id, "tag": tag,
            "column_id": column_id, "lane_id": lane_id, "condition": condition,
            "due_date_before": due_before, "due_date_after": due_after,
        }.items() if value is not None}
        if created_after is not None:
            filters["created_after"] = created_after.isoformat()
        if query is None and not filters:
            raise KaitenError("query_invalid", "Provide a search query or at least one card filter.")
        card_id = None
        if query is not None:
            card_id = self._card_reference(query, policy.tenant)
            filters.update({"query": query} if card_id is None else {"ids": str(card_id)})
        if compact:
            return self._compact_cards(policy, boards, filters, card_id=card_id, created_after=created_after)
        return self._list_cards(policy, boards, filters, card_id=card_id, include_metadata=include_metadata,
                                created_after=created_after)

    @staticmethod
    def _created_filter(value):
        if value is None:
            return None
        timestamp = _timestamp(value)
        if timestamp is None:
            raise KaitenError("query_invalid", "Use an ISO 8601 creation timestamp with a time zone.")
        return timestamp

    def _compact_cards(self, policy, boards, filters, *, card_id=None, created_after=None):
        summaries = []
        for board in boards:
            summary = {"board_id": board, "card_ids": [], "count": 0, "complete": False}
            try:
                page = self._list_cards(policy, (board,), filters, card_id=card_id, include_metadata=False,
                                        created_after=created_after, lean=True)
                card_ids = [card["id"] for card in page["cards"]]
                summary.update(card_ids=card_ids, count=len(card_ids), complete=page["complete"])
            except KaitenError as exc:
                if exc.code == "policy_changed":
                    raise
                summary["error"] = exc.code
            summaries.append(summary)
        self._check_policy(policy)
        return {"boards": summaries, "complete": all(board["complete"] for board in summaries)}

    def view_card(self, reference: str, *, board_id: int | None = None, with_related=False, with_links=False,
                  with_creator=False, activity_spaces=()):
        policy = load_policy(self.store)
        policy.assert_allowed("cards.read", board_id)
        card_id = self._card_reference(reference, policy.tenant)
        boards = (board_id,) if board_id is not None else policy.board_allowlist
        with_creator = with_creator or bool(activity_spaces)
        if card_id is None:
            if with_related or with_creator or with_links:
                raise KaitenError("query_invalid", "Use a numeric card ID or HTTPS card URL for related cards, links or creator.")
            return self._list_cards(policy, boards, {"query": reference}, include_metadata=True)
        if with_creator and not activity_spaces:
            match = re.match(r"/space/([1-9][0-9]*)/", urlsplit(reference).path)
            if not match:
                raise KaitenError("query_invalid", "Pass --activity-space or a card URL containing /space/ID/.")
            activity_spaces = (int(match.group(1)),)

        result = self._find_card_including_archive(policy, boards, card_id)
        if not result["cards"]:
            return result

        card = result["cards"][0]
        detail = self._card_detail(policy, card_id, card["board_id"])
        parents, parents_complete = self._related_cards(policy, detail, "parents", with_related)
        children, children_complete = self._related_cards(policy, detail, "children", with_related)
        card.update(parents=parents, children=children, relations_complete=parents_complete and children_complete)
        if with_creator:
            card.update(self._card_creator(policy, detail, activity_spaces))
        if with_links:
            linked_cards = [card, *parents, *children]
            placements = self._board_spaces(policy, {item["board_id"] for item in linked_cards})
            for item in linked_cards:
                item.update(self._card_link_fields(policy, item, placements))
        self._check_policy(policy)
        return result

    def _card_creator(self, policy, card, spaces):
        created = _timestamp(card.get("created"))
        if created is None:
            return {"creator_id": None, "creator_status": "unknown", "creator_reason": "creation_time_missing"}
        authors = set()
        reason = "no_creation_event"
        for space in dict.fromkeys(spaces):
            self._check_policy(policy)
            try:
                authors.update(self._creation_authors(policy, space, card["id"], created))
            except KaitenError as exc:
                if exc.code == "policy_changed":
                    raise
                reason = exc.code
        if len(authors) == 1:
            return {"creator_id": authors.pop(), "creator_status": "verified"}
        if authors:
            reason = "conflicting_creation_events"
        return {"creator_id": None, "creator_status": "unknown", "creator_reason": reason}

    def _creation_authors(self, policy, space, card_id, created):
        params = {"actions": "card_add", "limit": 100, "created_after": created.isoformat(),
                  "created_before": created.isoformat()}
        authors = set()
        cursors = set()
        for _ in range(20):
            self._check_policy(policy)
            page = self._request(policy.profile, "GET", f"/spaces/{space}/activity", params=params)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list) or "next_cursor" not in page:
                raise KaitenError("bad_json", "Kaiten API returned an invalid activity page.")
            for event in page["items"]:
                if not isinstance(event, dict):
                    raise KaitenError("bad_json", "Kaiten API returned an invalid activity event.")
                if (event.get("action") == "card_add" and type(event.get("card_id")) is int
                        and event["card_id"] == card_id and event.get("space_id") in (None, space)
                        and type(event.get("board_id")) is int and event["board_id"] in policy.board_allowlist
                        and _timestamp(event.get("created")) == created
                        and type(event.get("author_id")) is int and event["author_id"] > 0):
                    authors.add(event["author_id"])
            cursor = page["next_cursor"]
            if cursor is None:
                return authors
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise KaitenError("bad_json", "Kaiten API returned an invalid activity cursor.")
            cursors.add(cursor)
            params["cursor"] = cursor
        raise KaitenError("page_limit", "Kaiten activity exceeded the page limit.")

    def _find_card_including_archive(self, policy, boards, card_id):
        filters = {"ids": str(card_id)}
        result = self._list_cards(policy, boards, filters, card_id=card_id, include_metadata=True)
        if result["status"] == "empty":
            return self._list_cards(policy, boards, {**filters, "condition": 2},
                                    card_id=card_id, include_metadata=True)
        return result

    def _check_policy(self, policy):
        if load_policy(self.store) != policy:
            raise KaitenError("policy_changed", "Local profile or board selection changed; retry the read.")

    def _card_detail(self, policy, card_id, board_id):
        self._check_policy(policy)
        policy.assert_allowed("cards.read", board_id)
        card = self._request(policy.profile, "GET", f"/cards/{card_id}")
        if (not isinstance(card, dict) or type(card.get("id")) is not int
                or card["id"] != card_id or not isinstance(card.get("title"), str)):
            raise KaitenError("bad_json", "Kaiten API returned an invalid card identity.")
        if type(card.get("board_id")) is not int or card["board_id"] != board_id:
            raise KaitenError("forbidden", "Card board changed; no out-of-scope data is returned.")
        return card

    def _related_cards(self, policy, detail, direction, with_related):
        related = detail.get(direction)
        if not isinstance(related, list):
            raise KaitenError("bad_json", "Kaiten API returned an invalid list of related cards.")
        ids = detail.get(direction + "_ids")
        if ids is not None and (not isinstance(ids, list) or any(type(item) is not int or item <= 0 for item in ids)):
            raise KaitenError("bad_json", "Kaiten API returned invalid related card IDs.")

        cards = {}
        complete = True
        for card in related:
            if (not isinstance(card, dict) or type(card.get("id")) is not int or card["id"] <= 0
                    or not isinstance(card.get("title"), str) or type(card.get("board_id")) is not int):
                raise KaitenError("bad_json", "Kaiten API returned an invalid related card.")
            card_id = card["id"]
            if card["board_id"] not in policy.board_allowlist:
                complete = False
                continue
            if card_id in cards:
                continue
            if with_related:
                try:
                    card = self._card_detail(policy, card_id, card["board_id"])
                except KaitenError as exc:
                    if exc.code in {"forbidden", "not_found"}:
                        complete = False
                        continue
                    raise
                selected = _scalar_fields(card, CARD_FIELDS.split(","))
                selected.update(_card_metadata(card))
            else:
                selected = _scalar_fields(card, CARD_LINK_FIELDS)
            cards[card_id] = {**selected, "url": f"https://{policy.tenant}/boards/card/{card_id}"}

        count = detail.get(direction + "_count")
        complete = (complete and all(card_id in cards for card_id in (ids or []))
                    and (type(count) is not int or count <= len(cards)))
        return list(cards.values()), complete

    def list_archived_cards(self, *, board_id: int):
        if type(board_id) is not int or board_id < 1:
            raise KaitenError("board_invalid", "Choose a positive board ID.")
        policy = load_policy(self.store)
        policy.assert_allowed("cards.read", board_id)
        return self._list_cards(policy, (board_id,), {"condition": 2}, include_metadata=True)

    def card_history(self, reference: str, *, board_id: int | None = None):
        policy = load_policy(self.store)
        policy.assert_allowed("cards.read", board_id)
        card_id = self._card_reference(reference, policy.tenant)
        if card_id is None:
            raise KaitenError("query_invalid", "Use a numeric card ID or HTTPS card URL.")
        boards = (board_id,) if board_id is not None else policy.board_allowlist
        result = self._find_card_including_archive(policy, boards, card_id)
        if not result["complete"]:
            raise KaitenError("incomplete", "Narrow the board before inspecting history.")
        if not result["cards"]:
            raise KaitenError("not_found", "Card not found on the requested allowed boards.")
        if load_policy(self.store) != policy:
            raise KaitenError("policy_changed", "Local profile or board selection changed; retry the read.")
        card = result["cards"][0]
        data = self._request(policy.profile, "GET", f"/cards/{card_id}/location-history")
        if not isinstance(data, list):
            raise KaitenError("bad_json", "Kaiten API card history has an unexpected shape.")

        ordered = sorted((_location_event(event, card_id) for event in data),
                         key=lambda item: (item[0], item[1]), reverse=True)
        history = [item[2] for item in ordered]
        events = [event for event in history if event["board_id"] in policy.board_allowlist]
        latest_archive = next((
            index for index, event in enumerate(history)
            if event["condition"] == 2
            and (index + 1 == len(history) or history[index + 1]["condition"] != 2)
        ), None)
        archived_at = None
        archived_from = None
        if latest_archive is not None and history[latest_archive]["board_id"] in policy.board_allowlist:
            archived_at = history[latest_archive]["changed"]
            if latest_archive + 1 < len(history):
                previous = history[latest_archive + 1]
                # Filter after selecting the predecessor so hidden events never change the origin.
                if previous["condition"] == 1 and previous["board_id"] in policy.board_allowlist:
                    archived_from = self._archive_location(policy, card, previous)
        if load_policy(self.store) != policy:
            raise KaitenError("policy_changed", "Local profile or board selection changed; retry the read.")
        return {"card_id": card_id, "events": events, "complete": len(events) == len(history),
                "archived_at": archived_at, "archived_from": archived_from}

    def _archive_location(self, policy, card, event):
        location = {key: event[key] for key in LOCATION_FIELDS}
        missing = []
        for name in ("column", "lane"):
            entity_id = location[name + "_id"]
            if entity_id is None:
                continue
            current = card.get(name, {})
            if card["board_id"] == location["board_id"] and current.get("id") == entity_id and current.get("title"):
                location[name + "_title"] = current["title"]
            else:
                missing.append(name)
        if missing:
            try:
                topology = self._topology(policy, location["board_id"])
            except KaitenError as exc:
                if exc.code in {"not_found", "forbidden"}:
                    return location
                raise
            for name in missing:
                title = next((item["title"] for item in topology[name + "s"]
                              if item["id"] == location[name + "_id"]), None)
                if title is not None:
                    location[name + "_title"] = title
        return location

    def _list_cards(self, policy, boards, filters, *, include_metadata, card_id=None, created_after=None, lean=False):
        self._verify_profile(policy.profile, policy.tenant)
        cards = []
        seen_ids = set()
        complete = True
        condition = filters.get("condition")
        for board in boards:
            for page in range(20):
                if lean:
                    self._check_policy(policy)
                params = {"board_id": board, "limit": 100, "offset": page * 100, **filters}
                if not lean:
                    params["additional_card_fields"] = "description"
                data = self._request(policy.profile, "GET", "/cards", params=params)
                if not isinstance(data, list):
                    raise KaitenError("bad_json", "Kaiten API card list has an unexpected shape.")
                for card in data:
                    if not isinstance(card, dict) or type(card.get("id")) is not int or not isinstance(card.get("title"), str):
                        raise KaitenError("bad_json", "Kaiten API card identity is invalid.")
                    if type(card.get("board_id")) is not int or card["board_id"] != board:
                        raise KaitenError("forbidden", "Kaiten API returned a card outside the requested board.")
                    if condition is not None and (type(card.get("condition")) is not int
                                                  or card["condition"] != condition):
                        raise KaitenError("bad_json", "Kaiten API returned a card outside the requested condition.")
                    if created_after is not None:
                        created = _timestamp(card.get("created"))
                        if created is None:
                            complete = False
                            continue
                        if created <= created_after:
                            continue
                    if (card_id is None or card["id"] == card_id) and card["id"] not in seen_ids:
                        seen_ids.add(card["id"])
                        fields = ("id", "board_id") if lean else CARD_FIELDS.split(",")
                        selected = {key: card.get(key) for key in fields}
                        if include_metadata:
                            selected.update(_card_metadata(card))
                        cards.append(selected)
                if card_id is not None and cards:
                    return {"status": "found", "cards": cards, "complete": complete}
                if len(data) < 100:
                    break
            else:
                complete = False
        return {"status": "found" if cards else ("empty" if complete else "incomplete"), "cards": cards, "complete": complete}

    @staticmethod
    def _card_reference(query, tenant):
        if not isinstance(query, str) or not query.strip() or "\x00" in query:
            raise KaitenError("query_invalid", "Provide a card ID, URL or nonempty text.")
        if re.fullmatch(r"[0-9]+", query):
            if int(query) <= 0:
                raise KaitenError("query_invalid", "Card ID must be positive.")
            return int(query)
        if "://" in query:
            parsed = urlsplit(query)
            if parsed.scheme != "https" or parsed.netloc.lower() != tenant:
                raise KaitenError("forbidden", "Card URL belongs to a different tenant or uses an unsafe origin.")
            match = re.fullmatch(r"/(?:space/[1-9][0-9]*/)?boards/card/([1-9][0-9]*)/?", parsed.path)
            if not match or parsed.query or parsed.fragment:
                raise KaitenError("query_invalid", "Use a numeric Kaiten card URL ending in /boards/card/ID.")
            return int(match.group(1))
        return None

    def _verify_profile(self, profile, tenant):
        credentials = self.credentials.resolve(profile, tenant)
        self._clients[profile] = self.client_factory(credentials)

    def _request(self, profile, method, path, *, params=None, body=None):
        return self._clients[profile].request(method, path, params=params, body=body)
