"""Direct official API client with board-scoped reads."""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .auth import CredentialStore
from .client import KaitenClient
from .policy import KaitenError, LocalConfigStore, load_policy, normalize_profile, normalize_tenant


CARD_FIELDS = "id,title,board_id,column_id,lane_id,state,description"


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

    def find_cards(self, query: str, *, board_id: int | None = None):
        policy = load_policy(self.store)
        policy.assert_allowed("cards.read", board_id)
        card_id = self._card_reference(query, policy.tenant)
        self._verify_profile(policy.profile, policy.tenant)
        cards = []
        complete = True
        for board in (board_id,) if board_id is not None else policy.board_allowlist:
            for page in range(20):
                params = {"board_id": board, "limit": 100, "offset": page * 100,
                          "additional_card_fields": "description"}
                if card_id is None:
                    params["query"] = query
                else:
                    params["ids"] = str(card_id)
                data = self._request(policy.profile, "GET", "/cards", params=params)
                if not isinstance(data, list):
                    raise KaitenError("bad_json", "Kaiten API card list has an unexpected shape.")
                for card in data:
                    if not isinstance(card, dict) or type(card.get("id")) is not int or not isinstance(card.get("title"), str):
                        raise KaitenError("bad_json", "Kaiten API card identity is invalid.")
                    if type(card.get("board_id")) is not int or card["board_id"] != board:
                        raise KaitenError("forbidden", "Kaiten API returned a card outside the requested board.")
                    if card_id is None or card["id"] == card_id:
                        compact = {key: card.get(key) for key in CARD_FIELDS.split(",")}
                        if not any(item["id"] == card["id"] for item in cards):
                            cards.append(compact)
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
