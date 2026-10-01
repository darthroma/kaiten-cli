import json
from click.testing import CliRunner
import pytest

from kaiten_cli.auth import CredentialStore
from kaiten_cli.cli import cli
from kaiten_cli.migration import migrate_setup
from kaiten_cli.policy import KaitenError, LocalConfigStore


def previous_setup(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    policy = LocalConfigStore(tmp_path / 'cli-all/kaiten.json')
    policy.save(profile='work', tenant='example.kaiten.ru', board_allowlist=[12, 13])
    credentials = CredentialStore(tmp_path / 'cli-all/kaiten-profiles.json')
    credentials.save_credentials('work', 'example.kaiten.ru', 'synthetic-old')
    return policy, credentials


def test_migration_preserves_scope_source_and_token_safety(tmp_path, monkeypatch):
    policy, credentials = previous_setup(tmp_path, monkeypatch)
    before = policy.path.read_bytes(), credentials.path.read_bytes()
    result = CliRunner().invoke(cli, ['setup-migrate', '--json'])
    assert result.exit_code == 0 and 'synthetic-old' not in result.output
    assert json.loads(result.output)['data']['board_count'] == 2
    assert LocalConfigStore().load() == policy.load()
    assert CredentialStore().resolve('work', 'example.kaiten.ru', use_environment=False).token == 'synthetic-old'
    assert before == (policy.path.read_bytes(), credentials.path.read_bytes())
    assert migrate_setup()['credentials_imported'] is False


def test_migration_never_overwrites_new_policy_or_credentials(tmp_path, monkeypatch):
    previous_setup(tmp_path, monkeypatch)
    native = LocalConfigStore()
    native.save(profile='other', tenant='other.kaiten.ru', board_allowlist=[99])
    before = native.path.read_bytes()
    with pytest.raises(KaitenError) as failure:
        migrate_setup()
    assert failure.value.code == 'configuration_exists'
    assert native.path.read_bytes() == before
    assert not CredentialStore().path.exists()


def test_migration_keeps_a_newer_native_token(tmp_path, monkeypatch):
    previous_setup(tmp_path, monkeypatch)
    CredentialStore().save_credentials('work', 'example.kaiten.ru', 'synthetic-new')
    assert migrate_setup()['credentials_imported'] is False
    assert CredentialStore().resolve('work', 'example.kaiten.ru', use_environment=False).token == 'synthetic-new'
