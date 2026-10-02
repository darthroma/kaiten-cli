# Changelog

## 0.2.0

- Upload local files to allowed cards and existing internal comments.
- Add repeatable `--file` to internal comment preparation and posting.
- Use restricted-access UUID routes, content-bound previews and metadata readback.
- Report partial comment/file results without retrying or duplicating the comment.
- Extend the companion skill with attachment workflows.

## 0.1.0 — 2026-10-01

- Independent Kaiten-only repository and Python package, command `kaiten`.
- Direct REST API, private local profiles, approved board scope, internal
  comments with mentions, same-board movement and verified readback.
- Bundled `kaiten-cli` agent skill and explicit migration from the previous
  local factory setup without loading the factory or exposing credentials.
- HTTP, mutation and installed-wheel tests; Linux/macOS GitHub checks and releases.
