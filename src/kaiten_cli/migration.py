"""Explicit local data migration from the previous factory installation."""

import os
from pathlib import Path

from .auth import CredentialStore
from .policy import KaitenError, LocalConfigStore


def migrate_setup():
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    previous = LocalConfigStore(base / "cli-all/kaiten.json").load()
    source = CredentialStore(base / "cli-all/kaiten-profiles.json")
    selected = source.resolve(previous.profile, previous.tenant, use_environment=False)
    destination = LocalConfigStore()
    credentials = CredentialStore()
    destination._check_path()
    credentials._check_path()
    if destination.path.exists() and destination.load() != previous:
        raise KaitenError("configuration_exists", "A different Kaiten setup already exists; migration will not overwrite it.")
    imported = False
    try:
        credentials.resolve(previous.profile, previous.tenant, use_environment=False)
    except KaitenError as exc:
        if exc.code != "auth":
            raise
        credentials.save_credentials(previous.profile, previous.tenant, selected.token)
        imported = True
    if not destination.path.exists():
        destination.save(profile=previous.profile, tenant=previous.tenant,
                         board_allowlist=previous.board_allowlist)
    return {"profile": previous.profile, "tenant": previous.tenant,
            "board_count": len(previous.board_allowlist), "token_available": True,
            "credentials_imported": imported, "source_preserved": True}
