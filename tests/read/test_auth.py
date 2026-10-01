"""Native local credentials, domain binding, migration and token-safe output."""

import json
import stat
from click.testing import CliRunner
import pytest

from kaiten_cli.cli import cli
from kaiten_cli.auth import CredentialStore
from kaiten_cli.policy import KaitenError


def test_native_profile_atomic_private_and_repr_safe(tmp_path, monkeypatch):
    monkeypatch.delenv('KAITEN_TOKEN', raising=False)
    store = CredentialStore(tmp_path / 'private/profiles.json')
    result = store.save_credentials('work', 'example.kaiten.ru', 'synthetic-secret')
    assert result['saved'] and 'synthetic-secret' not in str(result)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    credentials = store.resolve('work', 'example.kaiten.ru')
    assert credentials.token == 'synthetic-secret' and 'synthetic-secret' not in repr(credentials)
    with pytest.raises(KaitenError) as error:
        store.resolve('work', 'other.kaiten.ru')
    assert error.value.code == 'profile_mismatch'


def test_environment_token_requires_exact_tenant(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / 'private/profiles.json')
    monkeypatch.setenv('KAITEN_TOKEN', 'synthetic-env')
    for tenant in ('', 'other.kaiten.ru'):
        monkeypatch.setenv('KAITEN_DOMAIN', tenant)
        with pytest.raises(KaitenError):
            store.resolve('work', 'example.kaiten.ru')
    monkeypatch.setenv('KAITEN_DOMAIN', 'https://example.kaiten.ru')
    assert store.resolve('work', 'example.kaiten.ru').source == 'env'
    assert not store.path.exists()


def test_explicit_migration_has_no_dependency_and_never_overwrites(tmp_path):
    source = tmp_path / 'old.json'
    source.write_text(json.dumps({'profiles': {'work': {'domain': 'example', 'token': 'synthetic-old'}}}))
    store = CredentialStore(tmp_path / 'private/profiles.json')
    before = source.read_bytes()
    result = store.import_legacy('work', 'example.kaiten.ru', source=source)
    assert result['saved'] and source.read_bytes() == before and 'synthetic-old' not in str(result)
    store.save_credentials('work', 'example.kaiten.ru', 'synthetic-new')
    assert store.import_legacy('work', 'example.kaiten.ru', source=source)['already_configured']
    assert store.resolve('work', 'example.kaiten.ru', use_environment=False).token == 'synthetic-new'


def test_wrong_migration_domain_and_public_credentials_are_rejected(tmp_path):
    source = tmp_path / 'old.json'
    source.write_text(json.dumps({'profiles': {'work': {'domain': 'wrong', 'token': 'synthetic-old'}}}))
    store = CredentialStore(tmp_path / 'private/profiles.json')
    with pytest.raises(KaitenError):
        store.import_legacy('work', 'example.kaiten.ru', source=source)
    assert not store.path.exists()
    store.save_credentials('work', 'example.kaiten.ru', 'synthetic-private')
    store.path.chmod(0o644)
    with pytest.raises(KaitenError) as error:
        store.resolve('work', 'example.kaiten.ru')
    assert error.value.code == 'credential_permissions'


def test_cli_auth_stdin_and_status_never_echo_token(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'personal'))
    monkeypatch.delenv('KAITEN_TOKEN', raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ['auth', 'login', '--profile', 'work', '--tenant', 'example.kaiten.ru', '--token-stdin', '--json'], input='synthetic-stdin\n')
    assert result.exit_code == 0 and json.loads(result.output)['data']['saved']
    assert 'synthetic-stdin' not in result.output
    result = runner.invoke(cli, ['auth', 'status', '--profile', 'work', '--tenant', 'example.kaiten.ru', '--json'])
    assert result.exit_code == 0 and json.loads(result.output)['data']['token_available']
    assert 'synthetic-stdin' not in result.output
    result = runner.invoke(cli, ['auth', 'login', '--profile', 'work', '--tenant', 'example.kaiten.ru', '--json'])
    assert result.exit_code == 1 and json.loads(result.output)['error']['code'] == 'interactive_required'


def test_cancelled_prompt_has_json_error_and_saves_nothing(tmp_path, monkeypatch):
    import click
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'personal'))
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    def cancel(*args, **kwargs):
        raise click.Abort()
    monkeypatch.setattr(click, 'prompt', cancel)
    # CliRunner replaces stdin, so simulate a TTY on the injected stream too.
    from click.testing import _NamedTextIOWrapper
    monkeypatch.setattr(_NamedTextIOWrapper, 'isatty', lambda self: True)
    reply = CliRunner().invoke(cli, ['auth', 'login', '--profile', 'work', '--tenant', 'example.kaiten.ru', '--json'])
    assert reply.exit_code == 1 and json.loads(reply.output)['error']['code'] == 'auth_cancelled'
    assert not (tmp_path / 'personal/kaiten-cli/profiles.json').exists()


@pytest.mark.parametrize('tenant', ['https://[broken', 'http://example.kaiten.ru', 'https://example.kaiten.ru@evil.invalid'])
def test_malformed_tenants_stay_in_safe_json(tenant):
    reply = CliRunner().invoke(cli, ['auth', 'login', '--profile', 'work', '--tenant', tenant, '--token-stdin', '--json'], input='synthetic\n')
    assert reply.exit_code == 1 and json.loads(reply.output)['error']['code'] == 'tenant_invalid'


@pytest.mark.parametrize('path', ['/users', '/cards/33', 'https://evil.invalid/', '/boards/99/columns'])
def test_request_hatch_cannot_bypass_scope(tmp_path, monkeypatch, path):
    from kaiten_cli.policy import LocalConfigStore
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'personal'))
    LocalConfigStore().save(profile='work', tenant='example.kaiten.ru', board_allowlist=[12])
    reply = CliRunner().invoke(cli, ['request', 'get', path, '--json'])
    assert reply.exit_code == 1
    assert json.loads(reply.output)['error']['code'] in {'request_forbidden', 'forbidden'}
