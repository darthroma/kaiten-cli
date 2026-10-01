import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from kaiten_cli.cli import cli
from kaiten_cli.policy import KaitenError as CatalogError
from kaiten_cli.skills import install_skill


ROOT = Path(__file__).resolve().parents[1]


def test_skill_install_preserves_customizations_and_updates_only_on_request(tmp_path):
    first = install_skill(directory=tmp_path)
    target = Path(first["path"])
    assert target.read_bytes() == (ROOT / "src/kaiten_cli/SKILL.md").read_bytes()
    assert install_skill(directory=tmp_path)["status"] == "already-installed"
    target.write_text("local customization")
    with pytest.raises(CatalogError, match="different skill"):
        install_skill(directory=tmp_path)
    assert target.read_text() == "local customization"
    unrelated = target.parent / "local-notes.txt"
    unrelated.write_text("keep")
    install_skill(directory=tmp_path, force=True)
    assert target.read_bytes() == (ROOT / "src/kaiten_cli/SKILL.md").read_bytes()
    assert unrelated.read_text() == "keep"


@pytest.mark.parametrize("symlink_folder", [True, False])
def test_skill_install_never_overwrites_symlink_source(tmp_path, symlink_folder):
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "SKILL.md"
    original.write_text("keep source")
    directory = tmp_path / "skills"
    folder = directory / "kaiten-cli"
    directory.mkdir()
    if symlink_folder:
        folder.symlink_to(outside, target_is_directory=True)
    else:
        folder.mkdir()
        (folder / "SKILL.md").symlink_to(original)
    with pytest.raises(CatalogError) as exc:
        install_skill(directory=directory, force=True)
    assert exc.value.code == "skill_symlink"
    assert original.read_text() == "keep source"

