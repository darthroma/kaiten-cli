"""Token-safe first-party diagnostics; network checks are opt-in."""

from . import __version__
from .auth import CredentialStore
from .policy import KaitenError, LocalConfigStore


def doctor(store=None, *, online=False, credentials=None):
    store = store or LocalConfigStore()
    credentials = credentials or CredentialStore(tracked_root=store.tracked_root)
    report = {"service": "kaiten", "backend": "direct_api", "version": __version__,
              "healthy": False, "online": online, "token_available": False,
              "auth_source": "missing", "checks": []}
    try:
        policy = store.load()
        selected = credentials.resolve(policy.profile, policy.tenant)
        report.update(profile=policy.profile, tenant=policy.tenant,
                      board_count=len(policy.board_allowlist), token_available=True, auth_source=selected.source)
        report["checks"].append({"name": "local_setup", "status": "pass", "code": "configured"})
        if online:
            from .mutations import KaitenMutations
            KaitenMutations(store, credentials=credentials).board_info(policy.board_allowlist[0])
            report["checks"].append({"name": "api", "status": "pass", "code": "allowed_board_reachable"})
        report["healthy"] = True
    except KaitenError as exc:
        report["checks"].append({"name": "api" if online and report["token_available"] else "local_setup",
                                 "status": "fail", "code": exc.code})
        report["next"] = "Run kaiten setup and kaiten auth login in your own terminal."
    return report
