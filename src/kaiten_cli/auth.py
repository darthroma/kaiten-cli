"""First-party credential storage and explicit, token-silent migration."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import sys

from .policy import KaitenError, LocalConfigStore, normalize_profile, normalize_tenant, write_private_json


@dataclass(frozen=True)
class Credentials:
    profile: str
    tenant: str
    token: str = field(repr=False)
    source: str = "config"


def _token(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        raise KaitenError("auth", "Configure a valid API token locally with kaiten auth login.")
    return value


class CredentialStore(LocalConfigStore):
    def __init__(self, path=None, *, tracked_root=None):
        super().__init__(path, tracked_root=tracked_root)
        if path is None:
            self.path = self.path.with_name("profiles.json")

    def _profiles(self):
        self._check_path()
        try:
            if os.name != "nt" and stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise KaitenError("credential_permissions", "Credential file must be readable only by its owner (chmod 600).")
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError) as exc:
            raise KaitenError("credential_config_invalid", "Local credentials cannot be read safely.") from exc
        if not isinstance(data, dict) or set(data) != {"profiles"} or not isinstance(data["profiles"], dict):
            raise KaitenError("credential_config_invalid", "Local credential format is invalid.")
        return data["profiles"]

    def resolve(self, profile, tenant, *, use_environment=True):
        profile, tenant = normalize_profile(profile), normalize_tenant(tenant)
        if use_environment and os.environ.get("KAITEN_TOKEN"):
            domain = os.environ.get("KAITEN_DOMAIN", "")
            if not domain or normalize_tenant(domain) != tenant:
                raise KaitenError("profile_mismatch", "KAITEN_DOMAIN must match the selected tenant when KAITEN_TOKEN is set.")
            return Credentials(profile, tenant, _token(os.environ["KAITEN_TOKEN"]), "env")
        data = self._profiles().get(profile)
        if not isinstance(data, dict):
            raise KaitenError("auth", "Run kaiten auth login locally; never send a token to the agent.")
        if normalize_tenant(data.get("tenant")) != tenant:
            raise KaitenError("profile_mismatch", "Credential profile belongs to a different tenant.")
        return Credentials(profile, tenant, _token(data.get("token")))

    def save_credentials(self, profile, tenant, token):
        profile, tenant, token = normalize_profile(profile), normalize_tenant(tenant), _token(token)
        profiles = self._profiles()
        profiles[profile] = {"tenant": tenant, "token": token}
        write_private_json(self.path, {"profiles": profiles})
        return {"profile": profile, "tenant": tenant, "token_available": True, "auth_source": "config", "saved": True}

    def import_legacy(self, profile, tenant, *, source=None):
        """One-time data import; never imports or invokes another CLI's code."""
        profile, tenant = normalize_profile(profile), normalize_tenant(tenant)
        if source is None:
            if sys.platform == "darwin":
                source = Path.home() / "Library/Application Support/kaiten-cli/config.json"
            elif sys.platform == "win32":
                source = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "kaiten-cli/config.json"
            else:
                source = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kaiten-cli/config.json"
        source = Path(source)
        if not source.is_absolute() or source.is_symlink():
            raise KaitenError("credential_config_invalid", "Import requires an absolute regular local credential file.")
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            selected = data["profiles"][profile]
            domain, token = selected["domain"], selected["token"]
        except (OSError, ValueError, UnicodeError, KeyError, TypeError) as exc:
            raise KaitenError("credential_import_failed", "Selected local profile could not be imported; use auth login instead.") from exc
        if isinstance(domain, str) and "." not in domain and ":" not in domain:
            domain += ".kaiten.ru"
        if normalize_tenant(domain) != tenant:
            raise KaitenError("profile_mismatch", "Imported profile does not match the requested tenant.")
        # Existing first-party profiles are never overwritten by migration.
        if profile in self._profiles():
            self.resolve(profile, tenant, use_environment=False)
            return {"profile": profile, "tenant": tenant, "token_available": True, "saved": False, "already_configured": True}
        return self.save_credentials(profile, tenant, token)
