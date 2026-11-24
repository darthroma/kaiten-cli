# Changelog

## 0.5.0

- Read archive history and direct parent/child cards.
- Archive and restore individual cards on allowed boards with previews and readback.
- Require explicit consent when archiving may remove parent and child links.
- Search cards by responsible user, member, tag, location, archive state and due date.
- Find users, manage members and assign responsible users on existing cards.
- Edit card types and custom properties, and manage tags, checklists, links and blockers.
- Move cards between allowed boards and preview exact bulk edits and archival.
- Export searches to CSV or JSON and report overdue, blocked and assigned work.

## 0.4.0

- Edit card titles, descriptions and date-only deadlines on allowed boards.
- Clear descriptions or deadlines explicitly; leave omitted fields untouched.
- Preview before/after fields, reject stale proposals, skip no-op updates and verify one PATCH.
- Extend the companion skill with card editing.

## 0.3.0

- Create cards on allowed boards with explicit column/lane selection and dry-run previews.
- Support descriptions, date-only deadlines and exact responsible account resolution.
- Verify the returned card and assignment; report uncertain writes without duplicate creation.
- Extend the companion skill with card creation.

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
