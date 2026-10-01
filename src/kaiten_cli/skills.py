"""Install a bundled companion skill without overwriting local customizations."""

import os
from pathlib import Path
import tempfile

from .policy import KaitenError


def install_skill(*, directory=None, force=False):
    source = Path(__file__).with_name("SKILL.md")
    if not source.is_file():
        raise KaitenError("skill_unavailable", "This service has no bundled companion skill.")
    content = source.read_bytes()
    base = Path(directory).expanduser() if directory is not None else Path.home() / ".agents" / "skills"
    folder = base / "kaiten-cli"
    target = folder / "SKILL.md"
    if target.is_file() and target.read_bytes() == content:
        return {"name": folder.name, "path": str(target), "status": "already-installed"}
    if folder.is_symlink() or target.is_symlink():
        raise KaitenError("skill_symlink", "The existing skill is a symlink; update its source or choose another directory.")
    if target.exists() and not force:
        raise KaitenError("skill_exists", "A different skill already exists; review it before using --force.")
    folder.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, target)
        else:
            # Create exclusively, including when another process races us.
            os.link(temporary, target)
        return {"name": folder.name, "path": str(target), "status": "installed"}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
