"""Phase 1's commands: inbox, ingest, recut, and the devices that may hand a batch in through the drop.
"""
import sys

from .. import ingest
from ..ingest import recut
from ..store import sessions
from .common import _session, out


def cmd_inbox(a):
    rows = ingest.list_inbox()
    if a.json:
        return out(rows)
    if not rows:
        return print(f"inbox empty ({ingest.INBOX_DIR})")
    for b in rows:
        extra = f"  -> {b['session']}" if b.get("session") else ""
        prog = b.get("progress") or {}
        p = f"  {prog.get('step', '')} {prog.get('clip', '')} {prog.get('pct', '')}%".rstrip(" %") if prog else ""
        err = f"  ERROR {b['error']}" if b.get("error") else ""
        print(f"  {b['id']:30} {b['status']:9} {b.get('clips', '?'):>3} clips {b.get('logs', '?'):>2} logs{extra}{p}{err}")


def cmd_ingest(a):
    if sessions.remote():
        sys.exit("ingest runs where the data is: on the server (unset ACROSCOPE_URL there)")
    batches = a.batches or ingest.pending()
    if not batches:
        return print("nothing to ingest")
    failed = 0
    for b in batches:
        try:
            out(ingest.process_batch(b, log=lambda m: print(m, file=sys.stderr)))
        except Exception as e:  # noqa: BLE001
            print(f"{b}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
    if failed:
        sys.exit(1)


def cmd_devices(a):
    if a.action == "add":
        if not a.arg:
            sys.exit("devices add <name>")
        d = sessions.store().add_device(a.arg)
        print("the token is shown this once: the app is paired with the server's address and this token", file=sys.stderr)
        return out(d)
    if a.action == "revoke":
        if not a.arg:
            sys.exit("devices revoke <id>")
        return out({"revoked": ingest.drop.revoke(a.arg)})
    rows = sessions.store().devices()
    if a.json:
        return out(rows)
    if not rows:
        return print("no device paired (acroscope devices add <name>, or the configuration page's devices tab)")
    for d in rows:
        state = f"revoked {d['revoked_at'][:10]}" if d["revoked_at"] else "paired"
        seen = f"last seen {d['last_seen_at'][:10]}" if d["last_seen_at"] else "not seen yet"
        print(f"  {d['id']:12} {state:18} {d['name']}  (since {d['created_at'][:10]}, {seen})")


def cmd_recut(a):
    out(recut.recut(_session(a.session), a.clip, log=lambda m: print(m, file=sys.stderr)))


def add(sp) -> None:
    """This group's subcommands."""
    p = sp.add_parser("inbox", help="the batches in inbox/ and their status"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_inbox)
    p = sp.add_parser("ingest", help="consume inbox batches now (all pending ones, or the named ones); the server does this on its own")
    p.add_argument("batches", nargs="*"); p.set_defaults(f=cmd_ingest)
    p = sp.add_parser("recut", help="compress a clip again from its kept original (a cut that took real footage); its moments and matches move with it")
    p.add_argument("session"); p.add_argument("clip"); p.set_defaults(f=cmd_recut)
    p = sp.add_parser("devices", help="the devices that may upload through the drop: list them, add <name> (prints its token, once), revoke <id>")
    p.add_argument("action", nargs="?", choices=["add", "revoke"]); p.add_argument("arg", nargs="?"); p.add_argument("--json", action="store_true")
    p.set_defaults(f=cmd_devices)
