"""Scoped restricted uploads, with byte-bound previews and explicit partial outcomes."""

from dataclasses import dataclass
import hashlib
import mimetypes
import os
from pathlib import Path
import stat
from uuid import UUID

from .mutations import KaitenMutations, _approve
from .policy import KaitenError, load_policy


MAX_FILE_SIZE = 100 * 1024 * 1024  # Local bound; the server may impose a smaller one.
MAX_FILES = 10


def _uid(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        result = UUID(value)
        if not result.int:
            raise ValueError
        return str(result)
    except (ValueError, AttributeError) as exc:
        raise KaitenError("uid_required", "Kaiten did not return a valid UID; no legacy numeric upload is attempted.") from exc


@dataclass(frozen=True)
class LocalFile:
    metadata: dict
    data: bytes


def read_file(path):
    """Only read bounded regular files; pipe/device paths must not block the CLI."""
    fd = None
    try:
        selected = Path(path).expanduser().resolve()
        name = selected.name
        if not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise KaitenError("file_invalid", "File name must not contain control characters.")
        fd = os.open(selected, os.O_RDONLY | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise KaitenError("file_invalid", "Only regular local files can be uploaded.")
        if info.st_size > MAX_FILE_SIZE:
            raise KaitenError("file_too_large", "The CLI limits each upload to 100 MiB; Kaiten may have a lower limit.")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            data = handle.read(MAX_FILE_SIZE + 1)
        if len(data) > MAX_FILE_SIZE:
            raise KaitenError("file_too_large", "The CLI limits each upload to 100 MiB.")
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise KaitenError("file_unreadable", "The selected local file cannot be read.") from exc
    finally:
        if fd is not None:
            os.close(fd)
    return LocalFile({"path": str(selected), "name": name, "size": len(data),
                      "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                      "sha256": hashlib.sha256(data).hexdigest()}, data)


class KaitenFiles(KaitenMutations):
    def _target(self, reference, *, board_id=None, comment_id=None):
        if comment_id is not None and (type(comment_id) is not int or comment_id <= 0):
            raise KaitenError("comment_invalid", "Select a positive comment ID from comments list.")
        policy, card = self._card(reference, board_id, "files.upload")
        detail = self._request(policy.profile, "GET", f"/cards/{card['id']}")
        if (not isinstance(detail, dict) or type(detail.get("id")) is not int or detail["id"] != card["id"]
                or type(detail.get("board_id")) is not int or detail["board_id"] != card["board_id"]):
            raise KaitenError("forbidden", "Card identity or board changed while inspecting the upload target.")
        target = {"card_id": card["id"], "board_id": card["board_id"], "card_uid": _uid(detail.get("uid"))}
        if comment_id is not None:
            comments = self._comments(policy, card["id"])
            matches = [c for c in comments if c["id"] == comment_id and not c["deleted"]]
            if len(matches) != 1:
                raise KaitenError("comment_not_found", "Select one existing comment on this allowed card.")
            comment = matches[0]
            if comment["internal"] is not True:
                raise KaitenError("comment_external", "File attachments are limited to internal comments.")
            target.update(comment_id=comment_id, comment_uid=_uid(comment.get("uid")))
        if load_policy(self.store) != policy:
            raise KaitenError("policy_changed", "Board/profile policy changed during file inspection.")
        return policy, card, target

    @staticmethod
    def _path(target):
        path = f"/cards/{target['card_uid']}"
        if "comment_uid" in target:
            path += f"/comments/{target['comment_uid']}"
        return path + "/files"

    def list_files(self, reference, *, board_id=None, comment_id=None):
        policy, card, target = self._target(reference, board_id=board_id, comment_id=comment_id)
        if comment_id is None:
            detail = self._request(policy.profile, "GET", f"/cards/{card['id']}")
            if (not isinstance(detail, dict) or detail.get("id") != card["id"]
                    or detail.get("board_id") != target["board_id"] or _uid(detail.get("uid")) != target["card_uid"]):
                raise KaitenError("forbidden", "Card changed while reading attachments.")
            items = detail.get("files", [])
        else:
            comments = self._request(policy.profile, "GET", f"/cards/{card['id']}/comments")
            matches = [c for c in comments if isinstance(c, dict) and c.get("id") == comment_id] if isinstance(comments, list) else []
            if (len(matches) != 1 or matches[0].get("internal") is not True or matches[0].get("deleted")
                    or matches[0].get("card_id") != card["id"] or _uid(matches[0].get("uid")) != target["comment_uid"]):
                raise KaitenError("comment_not_found", "Internal comment changed while reading attachments.")
            comment = matches[0]
            items = comment.get("attacments", comment.get("attachments", comment.get("files", [])))
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise KaitenError("bad_json", "Kaiten attachment list has an unexpected shape.")
        if load_policy(self.store) != policy:
            raise KaitenError("policy_changed", "Board/profile selection changed while reading attachments.")
        # Include old and restricted attachments; URLs are intentionally discarded.
        fields = ("id", "name", "size", "mime_type", "type", "entity_type", "author_id", "author_uid")
        return {"card": card, "target": target, "files": [{k: item.get(k) for k in fields} for item in items], "complete": True}

    def _prepare_file(self, reference, path, *, board_id=None, comment_id=None):
        policy, card, target = self._target(reference, board_id=board_id, comment_id=comment_id)
        local = read_file(path)
        preview = {"card": card, "target": target, "file": local.metadata}
        preview["approval"] = self._digest(policy, "file", preview)
        return policy, preview, local

    def prepare_file(self, reference, path, *, board_id=None, comment_id=None):
        return self._prepare_file(reference, path, board_id=board_id, comment_id=comment_id)[1]

    def upload_file(self, reference, path, *, board_id=None, comment_id=None, authorization=None, approval=None):
        if authorization not in {"direct", "approved"}:
            raise KaitenError("authorization_required", "An exact user upload request or an approved preview is required.")
        policy, preview, local = self._prepare_file(reference, path, board_id=board_id, comment_id=comment_id)
        if authorization == "approved":
            _approve(preview["approval"], approval)
        token = self.credentials.resolve(policy.profile, policy.tenant).token
        _approve(self._digest(policy, "file", {k: v for k, v in preview.items() if k != "approval"}), preview["approval"])
        return self._upload_once(policy, preview["target"], local, token=token)

    @staticmethod
    def _file_matches(item, target, local, author_uid):
        if not isinstance(item, dict):
            return False
        try:
            _uid(item.get("id"))
            if _uid(item.get("card_uid")) != target["card_uid"] or _uid(item.get("author_uid")) != author_uid:
                return False
            if "comment_uid" in target and _uid(item.get("comment_uid")) != target["comment_uid"]:
                return False
        except KaitenError:
            return False
        return (item.get("entity_type") == ("comment" if "comment_uid" in target else "card")
                and item.get("name") == local.metadata["name"]
                and str(item.get("size")) == str(local.metadata["size"]))

    @staticmethod
    def _public_file(item):
        # Signed URLs are temporary credentials; do not include them in output.
        return {k: item.get(k) for k in ("id", "name", "size", "mime_type", "card_uid", "comment_uid", "entity_type")}

    def _upload_once(self, policy, target, local, *, token):
        current_policy, _, current_target = self._target(str(target["card_id"]), board_id=target["board_id"],
                                                       comment_id=target.get("comment_id"))
        if current_policy != policy or current_target != target:
            raise KaitenError("preview_changed", "The upload target changed; prepare again.")
        author = self._request(policy.profile, "GET", "/users/current")
        author_uid = _uid(author.get("uid") if isinstance(author, dict) else None)
        if load_policy(self.store) != policy:
            raise KaitenError("policy_changed", "The profile/board policy changed before upload.")
        if self.credentials.resolve(policy.profile, policy.tenant).token != token:
            raise KaitenError("preview_changed", "The upload account changed; prepare again.")
        if self._clients[policy.profile].credentials.token != token:
            raise KaitenError("preview_changed", "The HTTP account changed; prepare again.")
        path = self._path(target)
        reply = None
        error = None
        try:
            reply = self._clients[policy.profile].upload_file(path, name=local.metadata["name"],
                                                            data=local.data, mime_type=local.metadata["mime_type"])
        except KaitenError as exc:
            error = exc
        outcome = {"target": target, "file": local.metadata, "readback": False}
        if self._definite_rejection(error) or (error and error.details.get("status_code") == 413):
            outcome["status_code"] = error.details.get("status_code")
            if outcome["status_code"] == 403:
                outcome["hint"] = "Check your upload permissions and Company settings → Restricted file access; no legacy fallback is used."
            if outcome["status_code"] == 413:
                outcome["hint"] = "This file exceeds Kaiten's configured size limit."
            return self._outcome("not-applied", error=error, **outcome)
        if not self._file_matches(reply, target, local, author_uid):
            # Without a returned file UID a timeout cannot safely identify this upload.
            return self._outcome("ambiguous", error=error, **outcome)
        outcome["uploaded"] = self._public_file(reply)
        try:
            after_policy, _, after_target = self._target(str(target["card_id"]), board_id=target["board_id"],
                                                       comment_id=target.get("comment_id"))
            if (after_policy != policy or after_target != target
                    or self._clients[policy.profile].credentials.token != token):
                raise KaitenError("policy_changed", "Upload target changed before readback.")
            saved = self._request(policy.profile, "GET", path + "/" + _uid(reply["id"]))
            if not self._file_matches(saved, target, local, author_uid) or _uid(saved.get("id")) != _uid(reply["id"]):
                raise KaitenError("bad_json", "Uploaded file metadata did not match the target and local file.")
        except KaitenError as exc:
            return self._outcome("ambiguous", error=exc, **outcome)
        outcome.update(readback=True, uploaded=self._public_file(saved))
        return self._outcome("applied", **outcome)

    def _prepare_attached_comment(self, reference, text, *, mention=None, board_id=None, file_paths=()):
        if not file_paths or len(file_paths) > MAX_FILES:
            raise KaitenError("files_invalid", "Select between one and ten files per comment.")
        preview = super().prepare_comment(reference, text, mention=mention, board_id=board_id)
        policy, _, target = self._target(str(preview["card"]["id"]), board_id=preview["card"]["board_id"])
        loaded = []
        total = 0
        for path in file_paths:
            local = read_file(path)
            total += len(local.data)
            if total > MAX_FILE_SIZE:
                raise KaitenError("files_too_large", "A comment's combined attachments must fit within 100 MiB in this CLI.")
            loaded.append(local)
        metadata = [f.metadata for f in loaded]
        if len({f["path"] for f in metadata}) != len(metadata):
            raise KaitenError("files_invalid", "Do not repeat the same file in one comment.")
        preview.update(files=metadata, target=target)
        if preview["ready"]:
            preview.pop("approval", None)
            preview["approval"] = self._digest(policy, "comment-files", preview)
        return policy, preview, loaded

    def prepare_comment(self, reference, text, *, mention=None, board_id=None, file_paths=()):
        if not file_paths:
            return super().prepare_comment(reference, text, mention=mention, board_id=board_id)
        return self._prepare_attached_comment(reference, text, mention=mention, board_id=board_id, file_paths=file_paths)[1]

    def post_comment(self, reference, text, *, mention, approval, board_id=None, file_paths=()):
        if not file_paths:
            return super().post_comment(reference, text, mention=mention, approval=approval, board_id=board_id)
        policy, preview, loaded = self._prepare_attached_comment(reference, text, mention=mention, board_id=board_id,
                                                              file_paths=file_paths)
        if not preview["ready"]:
            raise KaitenError("mention_required", "Select the mention before posting an attached comment.")
        _approve(preview["approval"], approval)
        token = self.credentials.resolve(policy.profile, policy.tenant).token
        _approve(self._digest(policy, "comment-files", {k: v for k, v in preview.items() if k != "approval"}), preview["approval"])
        # Use the existing verified internal-comment workflow, with its own bound preview.
        base = super().prepare_comment(reference, text, mention=mention, board_id=board_id)
        if (base["card"] != preview["card"] or base["text"] != preview["text"] or base["mention"] != preview["mention"]
                or load_policy(self.store) != policy or self.credentials.resolve(policy.profile, policy.tenant).token != token):
            raise KaitenError("preview_changed", "The comment target or mention changed; prepare again.")
        result = super().post_comment(reference, text, mention=mention, approval=base["approval"], board_id=board_id)
        if result["status"] != "applied":
            return result
        completed = []
        try:
            for local in loaded:
                # Bind uploads to the exact verified new comment, never the `new` placeholder.
                target = {**preview["target"], "comment_id": result["comment"]["id"],
                          "comment_uid": _uid(result["comment"].get("uid"))}
                uploaded = self._upload_once(policy, target, local, token=token)
                if uploaded["status"] != "applied":
                    return {**result, "status": "partial", "retry_safe": False, "files": completed,
                            "failed_file": local.metadata, "upload_result": uploaded,
                            "remaining_files": [f.metadata for f in loaded[len(completed) + 1:]]}
                completed.append(uploaded)
        except KaitenError as exc:
            return {**result, "status": "partial", "retry_safe": False, "files": completed,
                    "failed_file": loaded[len(completed)].metadata,
                    "upload_error": {"code": exc.code, "message": exc.message},
                    "remaining_files": [f.metadata for f in loaded[len(completed) + 1:]]}
        return {**result, "files": completed}
