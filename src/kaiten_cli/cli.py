"""First-party Kaiten command registration over the official REST API."""

from dataclasses import asdict
from datetime import date
import json
import sys
import re
from pathlib import Path

import click

from .policy import KaitenError, LocalConfigStore, normalize_profile, normalize_tenant
from .read import KaitenAdapter
from .audit import KaitenAudit
from .mutations import KaitenMutations
from .files import KaitenFiles
from .creation import KaitenCreation
from .editing import KaitenEditing
from .resources import KaitenResources
from .reporting import card_report, cards_csv
from .auth import CredentialStore
from .diagnostics import doctor as check_setup


SETUP_INSTRUCTIONS = [
    "Run kaiten auth login --profile <PROFILE> --tenant <TENANT> in your own terminal. The token prompt is hidden; never send the token to the agent.",
    "For automation use KAITEN_TOKEN and KAITEN_DOMAIN matching the configured tenant, or auth login --token-stdin with a secret provider.",
    "This CLI calls the official Kaiten API directly. No separate Kaiten CLI installation is required.",
    "Run kaiten setup-discovery --profile <PROFILE> --tenant <TENANT> to read only space/board names and IDs.",
    "Choose boards yourself. Preview with kaiten setup --profile <PROFILE> --tenant <TENANT> --board <ID> (repeat --board); repeat with --confirm to save locally.",
    "Never broaden the allowlist unless the user explicitly approved the displayed old/new lists.",
]


def _store(ctx):
    return LocalConfigStore(tracked_root=ctx.obj["root"])


def _present(ctx, command_json, operation, *, format_text=None):
    as_json = bool(ctx.obj["json"] or command_json)
    try:
        result = operation()
        if result.get("status") in {"ambiguous", "not-applied", "partial"}:
            raise KaitenError("mutation_" + result["status"].replace("-", "_"),
                              "Check the readback result before any new write; do not automatically repeat this command.", result)
        if format_text is not None and not as_json:
            click.echo(format_text(result), nl=False)
            return
    except KaitenError as exc:
        if as_json:
            click.echo(json.dumps({"ok": False, "data": None, "error": {
                "code": exc.code, "message": exc.message, "details": exc.details}}, ensure_ascii=False))
        else:
            click.echo(f"Error [{exc.code}]: {exc.message}", err=True)
            if exc.code == "mutation_partial":
                click.echo(json.dumps(exc.details, ensure_ascii=False, indent=2), err=True)
        raise click.exceptions.Exit(1) from exc
    if as_json:
        click.echo(json.dumps({"ok": True, "data": result, "error": None}, ensure_ascii=False))
    else:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))


from . import __version__


def _emit(document):
    click.echo(json.dumps(document, ensure_ascii=False))


class EnvelopeGroup(click.Group):
    """Keep Click parsing errors inside the machine-readable contract."""

    def main(self, args=None, **kwargs):
        arguments = list(sys.argv[1:] if args is None else args)
        option_arguments = arguments[:arguments.index("--")] if "--" in arguments else arguments
        if "--json" not in option_arguments:
            return super().main(args=arguments, **kwargs)
        standalone = kwargs.pop("standalone_mode", True)
        try:
            result = super().main(args=arguments, standalone_mode=False, **kwargs)
        except click.ClickException as exc:
            # Click messages may quote supplied values, including credentials.
            _emit({"ok": False, "data": None, "error": {
                "code": "cli_usage_error",
                "message": "Invalid command arguments. Consult the command's --help.",
                "details": {},
            }})
            if standalone:
                raise SystemExit(exc.exit_code) from exc
            raise click.exceptions.Exit(exc.exit_code) from exc
        if standalone:
            raise SystemExit(result if isinstance(result, int) else 0)
        return result


@click.group("kaiten", cls=EnvelopeGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="kaiten")
@click.option("--json", "group_json", is_flag=True)
@click.pass_context
def kaiten(ctx, group_json):
    """Manage Kaiten cards, related resources and reports on allowed boards."""
    ctx.obj = {"root": Path(__file__).resolve().parents[2], "json": group_json}


@kaiten.group("auth")
def auth():
    """Manage local credentials without placing tokens in command arguments."""


def _credentials(ctx):
    return CredentialStore(tracked_root=ctx.obj["root"])


@auth.command("login")
@click.option("--profile", required=True)
@click.option("--tenant", required=True)
@click.option("--token-stdin", is_flag=True, help="Read one token from stdin instead of the hidden terminal prompt.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def auth_login(ctx, profile, tenant, token_stdin, command_json):
    """Save a first-party profile using a hidden token prompt or explicit stdin."""
    def operation():
        normalize_profile(profile)
        normalize_tenant(tenant)
        if token_stdin:
            token = sys.stdin.read(4097).rstrip("\r\n")
        else:
            if not sys.stdin.isatty():
                raise KaitenError("interactive_required", "Use a local terminal for the hidden token prompt, or explicitly pass --token-stdin.")
            try:
                token = click.prompt("Kaiten API token", hide_input=True, err=True)
            except click.Abort as exc:
                raise KaitenError("auth_cancelled", "Credential setup cancelled; no token was saved.") from exc
        return _credentials(ctx).save_credentials(profile, tenant, token)
    _present(ctx, command_json, operation)


@auth.command("import-profile")
@click.option("--profile", required=True)
@click.option("--tenant", required=True)
@click.option("--from", "source", type=click.Path(path_type=Path, dir_okay=False),
              help="Optional absolute path to the previous local profile JSON.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def import_profile(ctx, profile, tenant, source, command_json):
    """One-time local migration; no other CLI is loaded or invoked, no token is printed."""
    _present(ctx, command_json, lambda: _credentials(ctx).import_legacy(profile, tenant, source=source))


@auth.command("status")
@click.option("--profile", required=True)
@click.option("--tenant", required=True)
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def auth_status(ctx, profile, tenant, command_json):
    """Report profile/tenant and token availability without revealing credentials."""
    def operation():
        selected = _credentials(ctx).resolve(profile, tenant)
        return {"profile": selected.profile, "tenant": selected.tenant,
                "token_available": True, "auth_source": selected.source}
    _present(ctx, command_json, operation)


@kaiten.command("doctor")
@click.option("--online", is_flag=True, help="Also read topology of one allowed board to check API access.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def doctor_command(ctx, online, command_json):
    """Check local configuration; optionally verify API access on an allowed board."""
    def operation():
        report = check_setup(_store(ctx), online=online)
        if not report["healthy"]:
            raise KaitenError("doctor_failed", "Kaiten setup or API check failed.", report)
        return report
    _present(ctx, command_json, operation)


@kaiten.group("request")
def request():
    """Read supported API resources with the same board scope and safe output."""


@request.command("get")
@click.argument("path")
@click.option("--board", type=click.IntRange(min=1), help="Optional board hint for scoped card lookup.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def request_get(ctx, path, board, command_json):
    """Read /boards/ID/columns|lanes or /cards/ID/comments inside the allowlist."""
    def operation():
        topology = re.fullmatch(r"/boards/([1-9][0-9]*)/(columns|lanes)", path)
        comments = re.fullmatch(r"/cards/([1-9][0-9]*)/comments", path)
        adapter = KaitenMutations(_store(ctx))
        if topology:
            if board is not None and board != int(topology[1]):
                raise KaitenError("forbidden", "Board hint differs from the requested resource.")
            result = adapter.board_info(int(topology[1]))
            return {"path": path, "items": result[topology[2]]}
        if comments:
            return {"path": path, **adapter.list_comments(comments[1], board_id=board)}
        raise KaitenError("request_forbidden", "Only scoped board columns/lanes and card comments are supported by request get.")
    _present(ctx, command_json, operation)


@kaiten.command("setup")
@click.option("--profile")
@click.option("--tenant")
@click.option("--board", type=click.IntRange(min=1), multiple=True)
@click.option("--confirm", is_flag=True, help="Save the explicitly chosen local profile, tenant and board list.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def setup(ctx, profile, tenant, board, confirm, command_json):
    """Show token-safe local instructions, or preview/save an explicit allowlist."""
    def operation():
        if profile is None and tenant is None and not board and not confirm:
            return {"instructions": SETUP_INSTRUCTIONS, "saved": False}
        if not profile or not tenant or not board:
            raise KaitenError("setup_incomplete", "Specify profile, tenant and a nonempty --board list. Use auth login for the hidden token prompt; setup only selects board policy.")
        proposed = {"profile": normalize_profile(profile), "tenant": normalize_tenant(tenant), "board_allowlist": sorted(set(board))}
        store = _store(ctx)
        previous = None
        try:
            previous = asdict(store.load())
        except KaitenError as exc:
            if exc.code != "setup_required":
                raise
        if confirm:
            store.save(**proposed)
        return {"previous": previous, "proposed": proposed, "saved": confirm,
                "next": "Setup saved; reads remain limited to these boards." if confirm else "Obtain explicit user approval, then repeat with --confirm."}
    _present(ctx, command_json, operation)


@kaiten.command("setup-discovery")
@click.option("--profile", required=True)
@click.option("--tenant", required=True)
@click.option("--space", "space_ids", type=click.IntRange(min=1), multiple=True,
              help="List boards only in these spaces; repeat to select several.")
@click.option("--spaces-only", is_flag=True, help="List space IDs/titles without fetching boards.")
@click.option("--workers", type=click.IntRange(min=1, max=4), default=4, show_default=True,
              help="Maximum concurrent metadata reads.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def setup_discovery(ctx, profile, tenant, space_ids, spaces_only, workers, command_json):
    """Explicit setup-only metadata read; does not read cards or save an allowlist."""
    _present(ctx, command_json, lambda: KaitenAdapter(_store(ctx)).discover_boards(
        profile=profile, tenant=tenant, space_ids=space_ids, spaces_only=spaces_only, workers=workers))


@kaiten.group("cards")
def cards():
    """Create, edit and read cards strictly inside the local board allowlist."""


@cards.command("view")
@click.argument("reference")
@click.option("--board", type=click.IntRange(min=1))
@click.option("--with-related", is_flag=True, help="Include full fields for direct parent and child cards.")
@click.option("--with-links", is_flag=True, help="Resolve the space links for the card and its direct relations.")
@click.option("--with-creator", is_flag=True, help="Verify the creation event in a space activity log.")
@click.option("--activity-space", "activity_spaces", type=click.IntRange(min=1), multiple=True,
              help="Space ID for creator verification. Repeat if the card moved across spaces.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def view(ctx, reference, board, with_related, with_links, with_creator, activity_spaces, command_json):
    """Read a card by ID or HTTPS URL and show its direct parents and children."""
    _present(ctx, command_json, lambda: KaitenAdapter(_store(ctx)).view_card(
        reference, board_id=board, with_related=with_related, with_links=with_links, with_creator=with_creator,
        activity_spaces=activity_spaces))


def _search_options(function):
    for decorator in (
        click.option("--json", "command_json", is_flag=True),
        click.option("--created-after", help="Only cards created strictly after this ISO 8601 timestamp with a time zone."),
        click.option("--due-after", type=click.DateTime(formats=["%Y-%m-%d"])),
        click.option("--due-before", type=click.DateTime(formats=["%Y-%m-%d"])),
        click.option("--condition", type=click.Choice(["active", "archived"])),
        click.option("--lane", "lane_id", type=click.IntRange(min=1)),
        click.option("--column", "column_id", type=click.IntRange(min=1)),
        click.option("--tag", help="Tag name."),
        click.option("--member", "member_id", type=click.IntRange(min=1), help="Card member user ID."),
        click.option("--responsible", "responsible_id", type=click.IntRange(min=1), help="Responsible user ID."),
        click.option("--board", type=click.IntRange(min=1), multiple=True, help="Repeat to select several boards."),
        click.argument("query", required=False),
    ):
        function = decorator(function)
    return function


def _search_cards(ctx, query, *, board, condition, due_before, due_after, **filters):
    return KaitenAdapter(_store(ctx)).find_cards(
        query, board_ids=board, include_metadata=True,
        condition={"active": 1, "archived": 2}.get(condition),
        due_before=due_before.date().isoformat() if due_before else None,
        due_after=due_after.date().isoformat() if due_after else None, **filters)


@cards.command("search")
@_search_options
@click.option("--compact", is_flag=True, help="Return only IDs, count and completeness for each board.")
@click.pass_context
def search(ctx, query, command_json, compact, **options):
    """Find cards by text, ID/URL, or filters inside permitted boards."""
    def operation():
        result = _search_cards(ctx, query, compact=compact, **options)
        return _board_read_result(result) if compact else result
    _present(ctx, command_json, operation)


def _board_read_result(result):
    if any("error" in board for board in result["boards"]):
        raise KaitenError("incomplete", "Some boards could not be read; see per-board results.", result)
    return result


@cards.command("audit-relations")
@click.option("--board", "board_ids", type=click.IntRange(min=1), required=True, multiple=True,
              help="Board with candidate cards. Repeat for several boards.")
@click.option("--ancestor-board", "ancestor_boards", type=click.IntRange(min=1), multiple=True,
              help="Require an ancestor on any of these boards. Repeat for several boards.")
@click.option("--created-after", help="Only cards created strictly after this ISO 8601 timestamp with a time zone.")
@click.option("--creator", "creator_id", type=click.IntRange(min=1), help="Require a verified creator user ID.")
@click.option("--activity-space", "activity_spaces", type=click.IntRange(min=1), multiple=True,
              help="Space ID for creator verification. Repeat for several spaces.")
@click.option("--with-links", is_flag=True, help="Include verified space links for the reported cards.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def audit_relations(ctx, command_json, **options):
    """Check direct parents or ancestor chains for active cards on selected boards."""
    _present(ctx, command_json, lambda: _board_read_result(KaitenAudit(_store(ctx)).audit_relations(**options)))


@kaiten.group("links")
def links():
    """Resolve shareable links to Kaiten cards and spaces."""


@links.command("card")
@click.argument("reference")
@click.option("--board", "board_id", type=click.IntRange(min=1), help="Known board of the card.")
@click.option("--space", "space_id", type=click.IntRange(min=1), help="Select and verify a specific space for the card.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def link_card(ctx, reference, board_id, space_id, command_json):
    """Resolve the current space link for a card ID or URL."""
    _present(ctx, command_json, lambda: KaitenAdapter(_store(ctx)).card_link(
        reference, board_id=board_id, space_id=space_id), format_text=lambda result: result["url"] + "\n")


@links.command("space")
@click.argument("space_id", type=click.IntRange(min=1))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def link_space(ctx, space_id, command_json):
    """Verify a space ID and return its boards URL."""
    _present(ctx, command_json, lambda: KaitenAdapter(_store(ctx)).space_link(space_id),
             format_text=lambda result: result["url"] + "\n")


@cards.command("export")
@_search_options
@click.pass_context
def export_cards(ctx, query, command_json, **options):
    """Export search results to CSV, or use --json for structured results."""
    def csv_output(result):
        if not result['complete']:
            raise KaitenError('incomplete', 'Narrow the export or inspect partial results using --json.')
        return cards_csv(result)

    if query is None and not any(value is not None for key, value in options.items() if key != 'board'):
        options['condition'] = 'active'
    _present(ctx, command_json, lambda: _search_cards(ctx, query, **options), format_text=csv_output)


@cards.command("report")
@click.option("--board", type=click.IntRange(min=1))
@click.option("--kind", type=click.Choice(["all", "overdue", "blocked", "responsible"]), default="all")
@click.option("--as-of", type=click.DateTime(formats=["%Y-%m-%d"]), help="Compare deadline dates to this day; defaults to today.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def report_cards(ctx, board, kind, as_of, command_json):
    """Report overdue cards, blockers and counts by responsible user."""
    def operation():
        result = KaitenAdapter(_store(ctx)).find_cards(None, board_id=board, include_metadata=True, condition=1)
        return card_report(result, as_of.date() if as_of else date.today(), kind)
    _present(ctx, command_json, operation)


@cards.command("catalog")
@click.argument("resource", type=click.Choice(["types", "properties"]))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def card_catalog(ctx, resource, command_json):
    """Look up card type and custom property IDs."""
    _present(ctx, command_json, lambda: KaitenResources(_store(ctx)).catalog(resource))


@cards.command("archive")
@click.option("--board", required=True, type=click.IntRange(min=1))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def card_archive(ctx, board, command_json):
    """List archived cards on one allowed board."""
    _present(ctx, command_json, lambda: KaitenAdapter(_store(ctx)).list_archived_cards(board_id=board))


@cards.command("history")
@click.argument("reference")
@click.option("--board", type=click.IntRange(min=1))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def card_history(ctx, reference, board, command_json):
    """Read a card's location history and latest archive origin on allowed boards."""
    _present(ctx, command_json, lambda: KaitenAdapter(_store(ctx)).card_history(reference, board_id=board))


@cards.command("create")
@click.option("--title", required=True, help="Exact card title (1–1024 characters).")
@click.option("--board", required=True, type=click.IntRange(min=1))
@click.option("--column", required=True, type=click.IntRange(min=1))
@click.option("--lane", type=click.IntRange(min=1), help="Required when the board has several lanes.")
@click.option("--description", help="Exact description, up to 32768 characters.")
@click.option("--due-date", help="Deadline in YYYY-MM-DD format, without a time.")
@click.option("--responsible", help="Exact full name, @username or numeric user ID.")
@click.option("--authorization", type=click.Choice(["direct", "approved"]),
              help="direct: exact user request; approved: accepted dry-run preview. Required for writes.")
@click.option("--approval", help="Hash from --dry-run; required with --authorization approved.")
@click.option("--dry-run", is_flag=True, help="Inspect the target and return a preview without creating a card.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def card_create(ctx, title, board, column, lane, description, due_date, responsible,
                authorization, approval, dry_run, command_json):
    """Create one card, verify its fields and return its ID and link. Never retries."""
    def operation():
        adapter = KaitenCreation(_store(ctx))
        options = dict(board_id=board, column_id=column, lane_id=lane, description=description,
                       due_date=due_date, responsible=responsible)
        if dry_run:
            return {"dry_run": True, **adapter.prepare_card(title, **options)}
        return adapter.create_card(title, authorization=authorization, approval=approval, **options)
    _present(ctx, command_json, operation)


def _edit_options(function):
    for decorator in (
        click.option("--json", "command_json", is_flag=True),
        click.option("--dry-run", is_flag=True, help="Show the current fields and selected changes without PATCH."),
        click.option("--approval", help="Hash from --dry-run."),
        click.option("--allow-unlink", is_flag=True, help="Allow archiving when Kaiten may remove parent/child links."),
        click.option("--restore", is_flag=True, help="Return archived cards to their boards."),
        click.option("--archive", is_flag=True, help="Send cards to the archive."),
        click.option("--property", "properties", multiple=True, help="Set a custom property as ID=JSON; null clears it."),
        click.option("--type", "type_id", type=click.IntRange(min=1), help="New card type ID."),
        click.option("--clear-due-date", is_flag=True, help="Remove the current deadline."),
        click.option("--due-date", help="Set a deadline in YYYY-MM-DD format, without a time."),
        click.option("--description", help="Replace the whole description; use an empty string to clear it."),
        click.option("--title", help="Replace the title (1–1024 characters)."),
        click.option("--board", "board_id", type=click.IntRange(min=1), help="Known board ID to limit lookup."),
    ):
        function = decorator(function)
    return function


def _edit_input(archive, restore, allow_unlink, **options):
    if archive and restore:
        raise KaitenError("archive_options_invalid", "Choose either --archive or --restore.")
    if allow_unlink and not archive:
        raise KaitenError("archive_options_invalid", "Use --allow-unlink only with --archive.")
    return {**options, "condition": 2 if archive else (1 if restore else None)}


@cards.command("update")
@click.argument("reference")
@_edit_options
@click.option("--authorization", type=click.Choice(["direct", "approved"]),
              help="direct: exact user edit request; approved: accepted dry-run preview. Required for writes.")
@click.pass_context
def card_update(ctx, reference, allow_unlink, authorization, approval, dry_run, command_json, **fields):
    """Edit, archive or restore one card by ID/URL and verify the result."""
    def operation():
        options = _edit_input(allow_unlink=allow_unlink, **fields)
        adapter = KaitenEditing(_store(ctx))
        if dry_run:
            return {"dry_run": True, **adapter.prepare_update(reference, **options)}
        return adapter.update_card(reference, authorization=authorization, approval=approval,
                                   allow_unlink=allow_unlink, **options)
    _present(ctx, command_json, operation)


@cards.command("bulk-update")
@click.argument("references", nargs=-1, required=True)
@_edit_options
@click.pass_context
def card_bulk_update(ctx, references, allow_unlink, approval, dry_run, command_json, **fields):
    """Preview and apply one change to an explicit list of cards."""
    _present(ctx, command_json, lambda: KaitenEditing(_store(ctx)).bulk_update(
        references, dry_run=dry_run, approval=approval, allow_unlink=allow_unlink,
        **_edit_input(allow_unlink=allow_unlink, **fields)))


@kaiten.group("boards")
def boards():
    """Inspect column/lane IDs on allowed boards."""


@boards.command("info")
@click.argument("board", type=click.IntRange(min=1))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def board_info(ctx, board, command_json):
    _present(ctx, command_json, lambda: KaitenMutations(_store(ctx)).board_info(board))


@kaiten.group("users")
def users():
    """Find active Kaiten accounts."""


@users.command("find")
@click.argument("query")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def users_find(ctx, query, command_json):
    """Find by name, exact @username or user ID."""
    _present(ctx, command_json, lambda: KaitenMutations(_store(ctx)).find_users(query))


def _resource_options(function):
    for decorator in (
        click.option("--json", "command_json", is_flag=True),
        click.option("--board", "board_id", type=click.IntRange(min=1), help="Known board of the card."),
        click.option("--dry-run", is_flag=True, help="Show the planned change without writing."),
        click.option("--approval", help="Hash from the reviewed --dry-run."),
        click.option("--authorization", type=click.Choice(["direct", "approved"]),
                     help="Required for writes: exact user request or an approved preview."),
    ):
        function = decorator(function)
    return function


@kaiten.command("members")
@click.argument("action", type=click.Choice(["list", "add", "assign", "remove"]))
@click.argument("reference", metavar="CARD")
@click.argument("user", required=False)
@_resource_options
@click.pass_context
def card_members(ctx, action, reference, user, command_json, **options):
    """List or change participants and the responsible user on a card."""
    def operation():
        if (action == "list") != (user is None):
            raise KaitenError("user_invalid", "Pass a user ID, exact name or @username for add, assign or remove.")
        return KaitenResources(_store(ctx)).members(reference, action=action, user=user, **options)
    _present(ctx, command_json, operation)


@kaiten.command("tags")
@click.argument("action", type=click.Choice(["list", "add", "remove"]))
@click.argument("reference", metavar="CARD")
@click.argument("tag", required=False)
@_resource_options
@click.pass_context
def card_tags(ctx, action, reference, tag, command_json, **options):
    """List card tags, add by name or remove by tag ID."""
    def operation():
        if (action == "list") != (tag is None):
            raise KaitenError("tag_invalid", "Pass a name for add, or a tag ID for remove.")
        if action == "remove" and not re.fullmatch(r"[1-9][0-9]*", tag):
            raise KaitenError("tag_invalid", "Choose a positive tag ID from tags list.")
        return KaitenResources(_store(ctx)).tags(reference, action=action, name=tag if action == "add" else None,
                                                tag_id=int(tag) if action == "remove" else None, **options)
    _present(ctx, command_json, operation)


@kaiten.command("relations")
@click.argument("action", type=click.Choice(["add", "remove"]))
@click.argument("parent")
@click.argument("child")
@click.option("--child-board", "child_board_id", type=click.IntRange(min=1))
@_resource_options
@click.pass_context
def card_relations(ctx, action, parent, child, command_json, **options):
    """Link or unlink a parent and child on allowed boards."""
    _present(ctx, command_json, lambda: KaitenResources(_store(ctx)).relation(
        parent, child, remove=action == "remove", **options))


@kaiten.command("blockers")
@click.argument("action", type=click.Choice(["list", "add", "update", "release"]))
@click.argument("reference", metavar="CARD")
@click.option("--reason")
@click.option("--blocker", help="Card ID or URL that blocks this card.")
@click.option("--blocker-board", "blocker_board_id", type=click.IntRange(min=1))
@click.option("--block-id", type=click.IntRange(min=1), help="Block ID from blockers list.")
@_resource_options
@click.pass_context
def card_blockers(ctx, action, reference, reason, blocker, blocker_board_id, block_id, command_json, **options):
    """List, add, edit or release individual card blockers."""
    def operation():
        if (((action in {"update", "release"}) != (block_id is not None))
                or (action in {"list", "release"} and any(value is not None for value in (reason, blocker, blocker_board_id)))
                or (blocker_board_id is not None and blocker is None)):
            raise KaitenError("blocker_invalid", "Use --reason or --blocker for changes, and --block-id for update or release.")
        return KaitenResources(_store(ctx)).blockers(
            reference, action=action, reason=reason, blocker=blocker, blocker_board_id=blocker_board_id,
            block_id=block_id, **options)
    _present(ctx, command_json, operation)


@kaiten.command("checklists")
@click.argument("action", type=click.Choice(["list", "add", "update", "remove", "item-add", "item-update", "item-remove"]))
@click.argument("reference", metavar="CARD")
@click.option("--checklist", "checklist_id", type=click.IntRange(min=1))
@click.option("--item", "item_id", type=click.IntRange(min=1))
@click.option("--name", help="Checklist name.")
@click.option("--text", help="Checklist item text.")
@click.option("--checked/--unchecked", default=None, help="Checklist item completion state.")
@click.option("--due-date", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--clear-due-date", is_flag=True)
@click.option("--responsible", "responsible_id", type=click.IntRange(min=1), help="Responsible user ID for an item.")
@click.option("--clear-responsible", is_flag=True)
@_resource_options
@click.pass_context
def card_checklists(ctx, action, reference, command_json, **options):
    """List, create, rename and remove checklists and checklist items."""
    def operation():
        required = {
            "list": (), "add": ("name",), "update": ("checklist_id", "name"), "remove": ("checklist_id",),
            "item-add": ("checklist_id", "text"), "item-update": ("checklist_id", "item_id"),
            "item-remove": ("checklist_id", "item_id"),
        }[action]
        item_fields = {"checked", "due_date", "responsible_id"}
        if action == "item-update":
            item_fields.update(("text", "clear_due_date", "clear_responsible"))
        allowed = set(required) | (item_fields if action in {"item-add", "item-update"} else set())
        fields = {"checklist_id", "item_id", "name", "text", "checked", "due_date", "responsible_id"}
        provided = {field for field in fields if options[field] is not None}
        provided.update(field for field in ("clear_due_date", "clear_responsible") if options[field])
        if not set(required) <= provided <= allowed:
            raise KaitenError("checklist_options_invalid", "Use checklist and item IDs and fields appropriate for the selected action.")
        if options["due_date"] is not None:
            options["due_date"] = options["due_date"].date().isoformat()
        return KaitenResources(_store(ctx)).checklists(reference, action=action, **options)
    _present(ctx, command_json, operation)


@kaiten.group("comments")
def comments():
    """Inspect or prepare/send an internal comment after the mention decision."""


@comments.command("list")
@click.argument("reference")
@click.option("--board", type=click.IntRange(min=1))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def comments_list(ctx, reference, board, command_json):
    _present(ctx, command_json, lambda: KaitenMutations(_store(ctx)).list_comments(reference, board_id=board))


def _comment_options(function):
    for decorator in (
        click.option("--json", "command_json", is_flag=True),
        click.option("--board", type=click.IntRange(min=1)),
        click.option("--file", "file_paths", multiple=True, type=click.Path(path_type=Path, dir_okay=False),
                     help="Attach a local file to this internal comment; repeat for up to ten files."),
        click.option("--mention", help="Explicit answer: 'нет'/'none' or exact name/@username."),
        click.option("--text", required=True, help="Exact UTF-8 draft without embedded @mentions."),
        click.argument("reference"),
    ):
        function = decorator(function)
    return function


@comments.command("prepare")
@_comment_options
@click.pass_context
def comment_prepare(ctx, reference, text, mention, board, command_json, file_paths):
    """Preview a draft and ask about @mention; with the answer return exact final text."""
    _present(ctx, command_json, lambda: KaitenFiles(_store(ctx)).prepare_comment(
        reference, text, mention=mention, board_id=board, file_paths=file_paths))


@comments.command("post")
@_comment_options
@click.option("--approval", required=True, help="Hash of the exact approved final prepare result.")
@click.option("--dry-run", is_flag=True, help="Return current final preview without POST.")
@click.pass_context
def comment_post(ctx, reference, text, mention, board, command_json, approval, dry_run, file_paths):
    """Send once with internal=true, then verify author/text/ID through readback."""
    def operation():
        adapter = KaitenFiles(_store(ctx))
        if mention is None:
            raise KaitenError("mention_required", "Answer the mention question through --mention before sending.")
        if dry_run:
            return {"dry_run": True, **adapter.prepare_comment(reference, text, mention=mention, board_id=board, file_paths=file_paths)}
        return adapter.post_comment(reference, text, mention=mention, approval=approval, board_id=board, file_paths=file_paths)
    _present(ctx, command_json, operation)


@kaiten.group("files")
def files():
    """Attach local files to allowed cards or existing internal comments."""


@files.command("list")
@click.argument("reference")
@click.option("--board", type=click.IntRange(min=1))
@click.option("--comment", type=click.IntRange(min=1))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def file_list(ctx, reference, board, comment, command_json):
    """Read attachment metadata without printing public or signed URLs."""
    _present(ctx, command_json, lambda: KaitenFiles(_store(ctx)).list_files(
        reference, board_id=board, comment_id=comment))


def _file_options(function):
    for decorator in (
        click.option("--json", "command_json", is_flag=True),
        click.option("--board", type=click.IntRange(min=1)),
        click.option("--comment", type=click.IntRange(min=1), help="Existing internal comment ID; omit to attach to the card."),
        click.option("--file", "file_path", required=True, type=click.Path(path_type=Path, dir_okay=False)),
        click.argument("reference"),
    ):
        function = decorator(function)
    return function


@files.command("prepare")
@_file_options
@click.pass_context
def file_prepare(ctx, reference, file_path, board, comment, command_json):
    """Inspect the card/comment and file size/hash, without uploading."""
    _present(ctx, command_json, lambda: KaitenFiles(_store(ctx)).prepare_file(
        reference, file_path, board_id=board, comment_id=comment))


@files.command("upload")
@_file_options
@click.option("--authorization", required=True, type=click.Choice(["direct", "approved"]),
              help="direct: exact user request; approved: user accepted this file/target preview.")
@click.option("--approval", help="Required with approved; hash from files prepare.")
@click.option("--dry-run", is_flag=True, help="Inspect the exact file and target without uploading.")
@click.pass_context
def file_upload(ctx, reference, file_path, board, comment, command_json, authorization, approval, dry_run):
    """Upload once using restricted access, then read back saved metadata."""
    def operation():
        adapter = KaitenFiles(_store(ctx))
        if dry_run:
            return {"dry_run": True, **adapter.prepare_file(reference, file_path, board_id=board, comment_id=comment)}
        return adapter.upload_file(reference, file_path, board_id=board, comment_id=comment,
                                   authorization=authorization, approval=approval)
    _present(ctx, command_json, operation)


@kaiten.group("move")
def move():
    """Prepare/apply a move within or between allowed boards."""


def _move_options(function):
    for decorator in (
        click.option("--json", "command_json", is_flag=True),
        click.option("--board", type=click.IntRange(min=1), help="Source board ID."),
        click.option("--to-board", type=click.IntRange(min=1), help="Target board ID for a cross-board move."),
        click.option("--lane", type=click.IntRange(min=1), help="Target lane; required across boards with multiple lanes."),
        click.option("--column", type=click.IntRange(min=1), required=True),
        click.argument("reference"),
    ):
        function = decorator(function)
    return function


@move.command("prepare")
@_move_options
@click.pass_context
def move_prepare(ctx, reference, column, lane, board, to_board, command_json):
    """Show card, source position, target position and approval hash."""
    _present(ctx, command_json, lambda: KaitenMutations(_store(ctx)).prepare_move(
        reference, column, lane_id=lane, board_id=board, to_board_id=to_board))


@move.command("apply")
@_move_options
@click.option("--authorization", type=click.Choice(["direct", "перемещай"]), required=True,
              help="direct: exact user request; перемещай: user approved this proposal.")
@click.option("--approval", help="Required for a proposal; hash of its prepare result.")
@click.option("--dry-run", is_flag=True, help="Show current preview without PATCH.")
@click.pass_context
def move_apply(ctx, reference, column, lane, board, to_board, command_json, authorization, approval, dry_run):
    """Apply one PATCH and verify the resulting location."""
    def operation():
        adapter = KaitenMutations(_store(ctx))
        if dry_run:
            return {"dry_run": True, **adapter.prepare_move(reference, column, lane_id=lane, board_id=board, to_board_id=to_board)}
        return adapter.apply_move(reference, column, lane_id=lane, board_id=board, to_board_id=to_board,
                                  authorization=authorization, approval=approval)
    _present(ctx, command_json, operation)


@kaiten.group("skill")
def skill():
    """Install the companion skill shipped with this Kaiten package."""


@skill.command("install")
@click.option("--directory", type=click.Path(path_type=Path, file_okay=False))
@click.option("--force", is_flag=True, help="Replace a reviewed local skill; never overwrites symlink sources.")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def install_skill_command(ctx, directory, force, command_json):
    from .skills import install_skill
    def operation():
        try:
            return install_skill(directory=directory, force=force)
        except OSError as exc:
            raise KaitenError("skill_install_failed", "Check the skill directory and permissions.") from exc
    _present(ctx, command_json, operation)


@kaiten.command("setup-migrate")
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def migrate_setup_command(ctx, command_json):
    """Explicitly import the previous cli-all Kaiten setup without exposing tokens."""
    from .migration import migrate_setup
    _present(ctx, command_json, migrate_setup)


cli = kaiten


def main():
    kaiten(prog_name="kaiten")


if __name__ == "__main__":
    main()
