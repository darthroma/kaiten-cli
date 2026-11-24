"""Direct official API client with board-scoped reads."""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
        spaces = []
        seen = set()
        for page in range(20):
            batch = self._metadata(self._request(profile, "GET", "/spaces", params={"limit": 100, "offset": page * 100}))
            if any(space["id"] in seen for space in batch):
                raise KaitenError("bad_json", "Space pagination returned a repeated identity.")
            seen.update(space["id"] for space in batch)
            spaces.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise KaitenError("incomplete", "Space discovery exceeded 2000 items; no partial setup was saved.")
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

    def find_cards(self, query: str, *, board_id: int | None = None, include_metadata=False):
        policy = load_policy(self.store)
        policy.assert_allowed("cards.read", board_id)
        card_id = self._card_reference(query, policy.tenant)
        boards = (board_id,) if board_id is not None else policy.board_allowlist
        filters = {"query": query} if card_id is None else {"ids": str(card_id)}
        return self._list_cards(policy, boards, filters, card_id=card_id, include_metadata=include_metadata)

    def view_card(self, reference: str, *, board_id: int | None = None, with_related=False):
        policy = load_policy(self.store)
        policy.assert_allowed("cards.read", board_id)
        card_id = self._card_reference(reference, policy.tenant)
        boards = (board_id,) if board_id is not None else policy.board_allowlist
        if card_id is None:
            if with_related:
                raise KaitenError("query_invalid", "Use a numeric card ID or HTTPS card URL with --with-related.")
            return self._list_cards(policy, boards, {"query": reference}, include_metadata=True)

        filters = {"ids": str(card_id)}
        result = self._list_cards(policy, boards, filters, card_id=card_id, include_metadata=True)
        if result["status"] == "empty":
            result = self._list_cards(policy, boards, {**filters, "condition": 2},
                                      card_id=card_id, include_metadata=True)
        if not result["cards"]:
            return result

        card = result["cards"][0]
        detail = self._card_detail(policy, card_id, card["board_id"])
        parents, parents_complete = self._related_cards(policy, detail, "parents", with_related)
        children, children_complete = self._related_cards(policy, detail, "children", with_related)
        if load_policy(self.store) != policy:
            raise KaitenError("policy_changed", "Local profile or board selection changed; retry the read.")
        card.update(parents=parents, children=children, relations_complete=parents_complete and children_complete)
        return result

    def _card_detail(self, policy, card_id, board_id):
        if load_policy(self.store) != policy:
            raise KaitenError("policy_changed", "Local profile or board selection changed; retry the read.")
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
        result = self._list_cards(policy, boards, {"ids": str(card_id)}, card_id=card_id, include_metadata=True)
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

    def _list_cards(self, policy, boards, filters, *, include_metadata, card_id=None):
        self._verify_profile(policy.profile, policy.tenant)
        cards = []
        complete = True
        condition = filters.get("condition")
        for board in boards:
            for page in range(20):
                params = {"board_id": board, "limit": 100, "offset": page * 100,
                          "additional_card_fields": "description", **filters}
                data = self._request(policy.profile, "GET", "/cards", params=params)
                if not isinstance(data, list):
                    raise KaitenError("bad_json", "Kaiten API card list has an unexpected shape.")
                for card in data:
                    if not isinstance(card, dict) or type(card.get("id")) is not int or not isinstance(card.get("title"), str):
                        raise KaitenError("bad_json", "Kaiten API card identity is invalid.")
                    if type(card.get("board_id")) is not int or card["board_id"] != board:
                        raise KaitenError("forbidden", "Kaiten API returned a card outside the requested board.")
                    if condition is not None and card.get("condition") != condition:
                        raise KaitenError("bad_json", "Kaiten API returned a card outside the archive.")
                    if (card_id is None or card["id"] == card_id) and not any(
                            item["id"] == card["id"] for item in cards):
                        selected = {key: card.get(key) for key in CARD_FIELDS.split(",")}
                        if include_metadata:
                            selected.update(_card_metadata(card))
                        cards.append(selected)
                if card_id is not None and cards:
                    return {"status": "found", "cards": cards, "complete": True}
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
