"""Fixed internal-comment and same-board move workflows over the official API.

Preview hashes bind exact inputs; they are cooperative authorization guardrails,
not credentials. A write is attempted once. Unknown outcomes never trigger retries.
"""

import hashlib
import hmac
import json
import re
import unicodedata

from .policy import KaitenError, load_policy
from .read import KaitenAdapter


def _digest(policy, operation, payload, token):
    document = {"profile": policy.profile, "tenant": policy.tenant,
                "operation": operation, "payload": payload}
    return hmac.new(token.encode(), json.dumps(document, sort_keys=True, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()


def _approve(expected, supplied):
    if not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
        raise KaitenError("preview_changed", "Prepare and review the current exact preview before applying it.")


def _normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class KaitenMutations(KaitenAdapter):
    def _digest(self, policy, operation, payload):
        selected = self.credentials.resolve(policy.profile, policy.tenant)
        return _digest(policy, operation, payload, selected.token)

    def _card(self, reference, board_id, action):
        policy = load_policy(self.store)
        policy.assert_allowed(action, board_id)
        result = self.find_cards(reference, board_id=board_id)
        if load_policy(self.store) != policy:
            raise KaitenError("policy_changed", "Local profile/board selection changed during inspection; prepare again.")
        if not result["complete"]:
            raise KaitenError("incomplete", "Narrow the board or query before preparing a mutation.")
        if len(result["cards"]) != 1:
            raise KaitenError("card_ambiguous" if result["cards"] else "not_found",
                              "Select exactly one allowed card.", {"cards": result["cards"]})
        card = result["cards"][0]
        policy.assert_allowed(action, card["board_id"])
        return policy, card

    def prepare_comment(self, reference, text, *, mention=None, board_id=None):
        if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text) > 4096:
            raise KaitenError("text_invalid", "Provide 1–4096 characters of comment text.")
        # Mentions must be selected separately so pasted content cannot silently
        # notify extra people. An email address does not match this syntax.
        if re.search(r"(?<![\w@])@[\w.-]+", text, flags=re.UNICODE):
            raise KaitenError("mention_in_text", "Choose mentions through --mention, rather than embedding them in the draft.")
        policy, card = self._card(reference, board_id, "comments.create")
        preview = {"card": card, "draft": text, "text": text, "internal": True,
                   "mention_question": "Нужно кого-то тегнуть? Ответьте «нет» или укажите имя/аккаунт.",
                   "mention": None, "ready": False}
        if mention is None:
            return preview
        if not isinstance(mention, str) or not mention.strip() or "\x00" in mention:
            raise KaitenError("mention_invalid", "Answer 'нет'/'none' or select an exact user name/account.")
        if _normalized(mention) not in {"нет", "none", "no"}:
            data = []
            seen = set()
            for page in range(20):
                batch = self._request(policy.profile, "GET", "/users", params={"limit": 100, "offset": page * 100})
                if not isinstance(batch, list):
                    raise KaitenError("bad_json", "Kaiten API user list has an unexpected shape.")
                for user in batch:
                    if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] in seen:
                        raise KaitenError("bad_json", "User pagination returned an invalid or repeated identity.")
                    seen.add(user["id"])
                data.extend(batch)
                if len(batch) < 100:
                    break
            else:
                raise KaitenError("incomplete", "User directory pagination is incomplete; no mention can be resolved safely.")
            users = []
            for user in data:
                if (not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0
                        or not isinstance(user.get("full_name"), str)
                        or not isinstance(user.get("username"), str)):
                    raise KaitenError("bad_json", "Kaiten API user identity has an unexpected shape.")
                if user.get("activated") is False:
                    continue
                users.append({key: user[key] for key in ("id", "full_name", "username")})
            query = _normalized(mention.removeprefix("@"))
            matches = [user for user in users if query in {_normalized(user["full_name"]), _normalized(user["username"])}]
            if len(matches) != 1:
                raise KaitenError("mention_ambiguous" if matches else "mention_not_found",
                                  "Select one exact account; no comment was sent.", {"candidates": matches or users})
            selected = matches[0]
            if not re.fullmatch(r"[\w.-]+", selected["username"], flags=re.UNICODE):
                raise KaitenError("mention_invalid", "The selected account cannot be represented as a safe @username.")
            preview["mention"] = selected
            preview["text"] = f"@{selected['username']} {text}"
        if len(preview["text"]) > 4096:
            raise KaitenError("text_invalid", "Final comment including mention exceeds 4096 characters.")
        preview["ready"] = True
        preview["approval"] = self._digest(policy, "comment", preview)
        return preview

    def _comments(self, policy, card_id):
        data = self._request(policy.profile, "GET", f"/cards/{card_id}/comments")
        if not isinstance(data, list):
            raise KaitenError("bad_json", "Kaiten API comment list has an unexpected shape.")
        result = []
        for item in data:
            if (not isinstance(item, dict) or type(item.get("id")) is not int
                    or item["id"] <= 0 or type(item.get("card_id")) is not int or item["card_id"] != card_id):
                raise KaitenError("bad_json", "Kaiten API comment identity has an unexpected shape.")
            result.append({key: item.get(key) for key in ("id", "card_id", "text", "author_id", "internal", "deleted")})
        return result

    def list_comments(self, reference, *, board_id=None):
        policy, card = self._card(reference, board_id, "cards.read")
        return {"card": card, "comments": self._comments(policy, card["id"])}

    def _write_once(self, policy, method, path, body):
        try:
            return self._request(policy.profile, method, path, body=body), None
        except KaitenError as exc:
            return None, exc

    @staticmethod
    def _outcome(status, *, error=None, **data):
        return {"status": status, "attempts": 1, "retry_safe": False,
                "error_code": error.code if error else None, **data}

    @staticmethod
    def _definite_rejection(error):
        # A missing readback is not proof a timed-out write failed.
        return error is not None and error.details.get("status_code") in {400, 401, 403, 404, 409, 422, 429}

    def post_comment(self, reference, text, *, mention, approval, board_id=None):
        preview = self.prepare_comment(reference, text, mention=mention, board_id=board_id)
        if not preview["ready"]:
            raise KaitenError("mention_required", "Answer the mention question before sending.")
        _approve(preview["approval"], approval)
        policy = load_policy(self.store)
        policy.assert_allowed("comments.create", preview["card"]["board_id"])
        _approve(self._digest(policy, "comment", {key: value for key, value in preview.items() if key != "approval"}), approval)
        current = self._request(policy.profile, "GET", "/users/current")
        if not isinstance(current, dict) or type(current.get("id")) is not int or current["id"] <= 0:
            raise KaitenError("bad_json", "Current author identity could not be verified.")
        card_id = preview["card"]["id"]
        baseline = {item["id"] for item in self._comments(policy, card_id)}
        reply, error = self._write_once(policy, "POST", f"/cards/{card_id}/comments",
                                       {"text": preview["text"], "type": 1, "internal": True})
        try:
            # Recheck scope before reading comments after the write.
            self._card(str(card_id), preview["card"]["board_id"], "cards.read")
            matches = [item for item in self._comments(policy, card_id) if item["id"] not in baseline
                and item["text"] == preview["text"] and item["author_id"] == current["id"]
                and item["internal"] is True and not item["deleted"]]
        except KaitenError as exc:
            return self._outcome("ambiguous", error=error or exc, card_id=card_id, readback=False)
        if isinstance(reply, dict) and type(reply.get("id")) is int:
            matches = [item for item in matches if item["id"] == reply["id"]]
        if len(matches) == 1 and not self._definite_rejection(error):
            return self._outcome("applied", error=error, card_id=card_id, comment=matches[0], readback=True)
        status = "not-applied" if self._definite_rejection(error) and not matches else "ambiguous"
        return self._outcome(status, error=error, card_id=card_id, readback=True)

    def _topology(self, policy, board_id):
        topology = {}
        for resource in ("columns", "lanes"):
            data = self._request(policy.profile, "GET", f"/boards/{board_id}/{resource}")
            items = self._metadata(data)
            if any(item.get("board_id", board_id) != board_id for item in data):
                raise KaitenError("forbidden", "Kaiten API returned topology from a different board.")
            topology[resource] = items
        return topology

    def board_info(self, board_id):
        policy = load_policy(self.store)
        policy.assert_allowed("cards.read", board_id)
        self._verify_profile(policy.profile, policy.tenant)
        return {"board_id": board_id, **self._topology(policy, board_id)}

    def prepare_move(self, reference, column_id, *, lane_id=None, board_id=None):
        if type(column_id) is not int or column_id <= 0 or (lane_id is not None and (type(lane_id) is not int or lane_id <= 0)):
            raise KaitenError("target_invalid", "Target column and lane IDs must be positive integers.")
        policy, card = self._card(reference, board_id, "cards.move")
        topology = self._topology(policy, card["board_id"])
        lane_id = card["lane_id"] if lane_id is None else lane_id
        column = next((item for item in topology["columns"] if item["id"] == column_id), None)
        lane = next((item for item in topology["lanes"] if item["id"] == lane_id), None)
        if column is None or lane is None:
            raise KaitenError("target_invalid", "Choose an existing column/lane on this card's allowed board.", topology)
        if any(type(card[key]) is not int or card[key] <= 0 for key in ("column_id", "lane_id")):
            raise KaitenError("bad_json", "Current card location could not be verified.")
        preview = {"card": card, "source": {key: card[key] for key in ("board_id", "column_id", "lane_id")},
                   "target": {"board_id": card["board_id"], "column": column, "lane": lane},
                   "confirmation": "перемещай"}
        preview["approval"] = self._digest(policy, "move", preview)
        return preview

    def apply_move(self, reference, column_id, *, lane_id=None, board_id=None, authorization=None, approval=None):
        if authorization not in {"direct", "перемещай"}:
            raise KaitenError("authorization_required", "Use direct only for an exact user request; proposals require 'перемещай' and the approved preview.")
        preview = self.prepare_move(reference, column_id, lane_id=lane_id, board_id=board_id)
        if authorization != "direct":
            _approve(preview["approval"], approval)
        target, card_id = preview["target"], preview["card"]["id"]
        expected = {"board_id": target["board_id"], "column_id": target["column"]["id"], "lane_id": target["lane"]["id"]}
        if preview["source"] == expected:
            return {"status": "already-applied", "attempts": 0, "retry_safe": False, "location": expected}
        policy = load_policy(self.store)
        policy.assert_allowed("cards.move", target["board_id"])
        _approve(self._digest(policy, "move", {key: value for key, value in preview.items() if key != "approval"}), preview["approval"])
        _, error = self._write_once(policy, "PATCH", f"/cards/{card_id}",
                                   {"column_id": expected["column_id"], "lane_id": expected["lane_id"]})
        try:
            _, card = self._card(str(card_id), target["board_id"], "cards.read")
            location = {key: card[key] for key in expected}
        except KaitenError as exc:
            return self._outcome("ambiguous", error=error or exc, card_id=card_id, readback=False)
        if location == expected:
            status = "applied" if not self._definite_rejection(error) else "ambiguous"
        else:
            status = "not-applied" if self._definite_rejection(error) else "ambiguous"
        return self._outcome(status, error=error, card_id=card_id, location=location, readback=True)
