# Kaiten CLI

This repository owns only the Kaiten client, command `kaiten`, companion skill,
tests and releases. Do not add the CLI factory or another service here.

Run `uv sync --locked`, `uv run pytest`, and `uv build` for verification.
Installed-wheel tests run outside the checkout without PYTHONPATH or any factory
package. Test writes use synthetic HTTP responses, never company data.

Use JSON envelopes and nonzero exits for failures. Never print credentials,
expand the configured board scope, or retry uncertain writes automatically.
Keep `skills/kaiten-cli/SKILL.md` and `src/kaiten_cli/SKILL.md` equal.
Release tags must match both project.version and kaiten_cli.__version__.
