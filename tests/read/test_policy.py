"""Policy contract through a temporary, external local-config store."""

import json
import stat

import pytest

from kaiten_cli.policy import KaitenError, LocalConfigStore, load_policy


def test_local_setup_is_atomic_private_and_rejects_empty_allowlist(tmp_path):
    tracked = tmp_path / "repository"
    tracked.mkdir()
    store = LocalConfigStore(tmp_path / "local" / "kaiten.json", tracked_root=tracked)
    saved = store.save(profile="test-profile", tenant="https://example.kaiten.ru", board_allowlist=[12, 12])
    assert saved.board_allowlist == (12,)
    assert load_policy(store).tenant == "example.kaiten.ru"
    assert json.loads(store.path.read_text()) == {
        "profile": "test-profile", "tenant": "example.kaiten.ru", "board_allowlist": [12]
    }
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    before = store.path.read_bytes()
    with pytest.raises(KaitenError, match="nonempty"):
        store.save(profile="test-profile", tenant="example.kaiten.ru", board_allowlist=[])
    assert store.path.read_bytes() == before
    with pytest.raises(KaitenError) as failure:
        LocalConfigStore(tracked / "personal.json", tracked_root=tracked).save(
            profile="test-profile", tenant="example.kaiten.ru", board_allowlist=[12]
        )
    assert failure.value.code == "local_config_in_repository"
    assert list(tracked.iterdir()) == []
