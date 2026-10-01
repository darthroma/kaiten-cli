"""Personal, atomic policy storage. No credentials are accepted or stored."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


class KaitenError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


def write_private_json(path, data):
    """Atomically replace a local file with mode 0600 without echoing its data."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".kaiten-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise KaitenError("local_config_write_failed", "Local setup could not be saved; the previous configuration is unchanged.") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def normalize_tenant(value: str) -> str:
    """Accept only HTTPS Kaiten tenant hosts, not URL paths or credentials."""
    if not isinstance(value, str):
        raise KaitenError("tenant_invalid", "Use a valid HTTPS Kaiten tenant host.")
    try:
        parsed = urlsplit(value.strip() if "://" in value else "https://" + value.strip())
    except ValueError as exc:
        raise KaitenError("tenant_invalid", "Use a valid HTTPS Kaiten tenant host.") from exc
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.netloc.lower() != host.lower()
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.kaiten\.ru", host.lower())):
        raise KaitenError("tenant_invalid", "Use a valid HTTPS Kaiten tenant host.")
    return host.lower()


@dataclass(frozen=True)
class KaitenPolicy:
    profile: str
    tenant: str
    board_allowlist: tuple[int, ...]

    def assert_allowed(self, action: str, resource: int | None = None) -> None:
        if not self.board_allowlist:
            raise KaitenError("allowlist_empty", "A nonempty board allowlist is required before any HTTP request.")
        if action not in {"cards.read", "comments.create", "cards.move"} or (resource is not None and resource not in self.board_allowlist):
            raise KaitenError("forbidden", "This action or board is outside the local allowlist.")


def normalize_profile(profile: str) -> str:
    if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", profile):
        raise KaitenError("profile_invalid", "Choose an explicit profile name using letters, numbers, dots, dashes or underscores.")
    return profile


def _validated(profile, tenant, board_allowlist) -> KaitenPolicy:
    profile = normalize_profile(profile)
    if not isinstance(board_allowlist, (list, tuple)) or not board_allowlist:
        raise KaitenError("allowlist_empty", "A nonempty board allowlist is required.")
    if any(type(board) is not int or board <= 0 for board in board_allowlist):
        raise KaitenError("allowlist_invalid", "Board IDs must be positive integers.")
    return KaitenPolicy(profile, normalize_tenant(tenant), tuple(sorted(set(board_allowlist))))


class LocalConfigStore:
    """Inject an external path for tests; production chooses a user-only location."""

    def __init__(self, path: Path | None = None, *, tracked_root: Path | None = None):
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.path = Path(path) if path is not None else base / "kaiten-cli" / "policy.json"
        self.tracked_root = tracked_root

    def _check_path(self):
        path = self.path.resolve()
        if not self.path.is_absolute():
            raise KaitenError("local_config_path_invalid", "Local config requires an absolute external path.")
        roots = [Path(__file__).resolve().parents[2]]
        if self.tracked_root is not None:
            roots.append(self.tracked_root.resolve())
        if any(path.is_relative_to(root) for root in roots):
            raise KaitenError("local_config_in_repository", "Personal configuration must remain outside the repository.")
        if self.path.is_symlink():
            raise KaitenError("local_config_path_invalid", "Local config may not be a symbolic link.")

    def load(self) -> KaitenPolicy:
        self._check_path()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KaitenError("setup_required", "Run kaiten setup locally; never send a token to the agent.") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise KaitenError("local_config_invalid", "Local configuration cannot be read safely.") from exc
        if not isinstance(raw, dict) or set(raw) != {"profile", "tenant", "board_allowlist"}:
            raise KaitenError("local_config_invalid", "Local configuration must contain only profile, tenant and board allowlist.")
        return _validated(**raw)

    def save(self, *, profile: str, tenant: str, board_allowlist: list[int] | tuple[int, ...]) -> KaitenPolicy:
        self._check_path()
        policy = _validated(profile, tenant, board_allowlist)
        write_private_json(self.path, {"profile": policy.profile, "tenant": policy.tenant,
                                     "board_allowlist": policy.board_allowlist})
        return policy


def load_policy(store: LocalConfigStore | None = None) -> KaitenPolicy:
    return (store or LocalConfigStore()).load()
