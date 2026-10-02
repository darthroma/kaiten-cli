"""First-party Kaiten command registration over the official REST API."""

from dataclasses import asdict
import json
import sys
import re
from pathlib import Path

import click

from .policy import KaitenError, LocalConfigStore, normalize_profile, normalize_tenant
from .read import KaitenAdapter
from .mutations import KaitenMutations
from .files import KaitenFiles
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


def _present(ctx, command_json, operation):
    as_json = bool(ctx.obj["json"] or command_json)
    try:
        result = operation()
        if result.get("status") in {"ambiguous", "not-applied", "partial"}:
            raise KaitenError("mutation_" + result["status"].replace("-", "_"),
                              "Check the readback result before any new write; do not automatically repeat this command.", result)
    except KaitenError as exc:
        if as_json:
            click.echo(json.dumps({"ok": False, "data": None, "error": {
                "code": exc.code, "message": exc.message, "details": exc.details}}, ensure_ascii=False))
        else:
            click.echo(f"Error [{exc.code}]: {exc.message}", err=True)
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
    """Kaiten: scoped reads, internal comments, file attachments and same-board moves."""
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
    """Read cards strictly inside the local board allowlist."""


@cards.command("view")
@click.argument("reference")
@click.option("--board", type=click.IntRange(min=1))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def view(ctx, reference, board, command_json):
    """Find a card by numeric ID or HTTPS URL, scanning only allowed boards."""
    _present(ctx, command_json, lambda: KaitenAdapter(_store(ctx)).find_cards(reference, board_id=board))


@cards.command("search")
@click.argument("query")
@click.option("--board", type=click.IntRange(min=1))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def search(ctx, query, board, command_json):
    """Search UTF-8 text (or an ID/URL) only inside permitted boards."""
    _present(ctx, command_json, lambda: KaitenAdapter(_store(ctx)).find_cards(query, board_id=board))


@kaiten.group("boards")
def boards():
    """Inspect column/lane IDs on allowed boards."""


@boards.command("info")
@click.argument("board", type=click.IntRange(min=1))
@click.option("--json", "command_json", is_flag=True)
@click.pass_context
def board_info(ctx, board, command_json):
    _present(ctx, command_json, lambda: KaitenMutations(_store(ctx)).board_info(board))


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
    """Prepare/apply a move inside the same allowed board."""


def _move_options(function):
    for decorator in (
        click.option("--json", "command_json", is_flag=True),
        click.option("--board", type=click.IntRange(min=1)),
        click.option("--lane", type=click.IntRange(min=1), help="Target lane; defaults to the current lane."),
        click.option("--column", type=click.IntRange(min=1), required=True),
        click.argument("reference"),
    ):
        function = decorator(function)
    return function


@move.command("prepare")
@_move_options
@click.pass_context
def move_prepare(ctx, reference, column, lane, board, command_json):
    """Show card, source position, target position and approval hash."""
    _present(ctx, command_json, lambda: KaitenMutations(_store(ctx)).prepare_move(
        reference, column, lane_id=lane, board_id=board))


@move.command("apply")
@_move_options
@click.option("--authorization", type=click.Choice(["direct", "перемещай"]), required=True,
              help="direct: exact user request; перемещай: user approved this proposal.")
@click.option("--approval", help="Required for a proposal; hash of its prepare result.")
@click.option("--dry-run", is_flag=True, help="Show current preview without PATCH.")
@click.pass_context
def move_apply(ctx, reference, column, lane, board, command_json, authorization, approval, dry_run):
    """Apply one same-board PATCH and verify resulting location."""
    def operation():
        adapter = KaitenMutations(_store(ctx))
        if dry_run:
            return {"dry_run": True, **adapter.prepare_move(reference, column, lane_id=lane, board_id=board)}
        return adapter.apply_move(reference, column, lane_id=lane, board_id=board,
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
