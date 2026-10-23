"""Phase 5's commands: share, unshare, shares.
"""
import sys

from .. import share
from .common import _session, fmt_time, out, parse_time


def cmd_share(a):
    """Publish one moment (#57): the clip around it with the sticks drawn over, on a secret link. Runs where the
    clips and the logs are (proxied to the server); prints the share with its url."""
    out(share.render(_session(a.session), a.moment, pad=a.pad, text=a.text, expires=a.expires, sticks=not a.no_sticks,
                     audio=a.audio, poster=parse_time(a.poster) if a.poster else None,
                     start=parse_time(a.start) if a.start else None, end=parse_time(a.end) if a.end else None,
                     token=a.update, log=lambda m: print(m, file=sys.stderr)))


def cmd_unshare(a):
    out(share.revoke(a.token, log=lambda m: print(m, file=sys.stderr)))


def cmd_shares(a):
    rows = share.list_shares(all=a.all)
    if a.json:
        return out(rows)
    if not rows:
        return print("no shares" + ("" if a.all else " (--all lists expired and revoked ones too)"))
    for r in rows:
        until = "never expires" if not r["expires_at"] else f"until {r['expires_at'][:16]}"
        print(f"{r['token']}  {r['state']:8} {r['session']} {r['clip'][-3:]} {fmt_time(r['start'])}-{fmt_time(r['end'])}  "
              f"{r['text'][:40]!r}  {until}  {r['url'] or '(no ACROSCOPE_SHARE_URL on the server)'}")


def add(sp) -> None:
    """This group's subcommands."""
    p = sp.add_parser("share", help="publish one moment on a secret link: the clip around it (no sound, no metadata) with the sticks drawn over it, as a static bundle")
    p.add_argument("session"); p.add_argument("moment", help="the moment's id (m12)")
    p.add_argument("--pad", type=float, default=2.0, help=f"seconds kept before and after the moment (default 2, at most {share.PAD_MAX:g})")
    p.add_argument("--text", help="the line shown under the clip (default: the moment's title; never the session or the date)")
    p.add_argument("--expires", help=f"30d, 12h, 2w or never (default {share.DEFAULT_EXPIRY}; with --update the share's own unless given): the bundle is taken down after that")
    p.add_argument("--no-sticks", action="store_true", help="the clip alone (a clip without a blackbox match)")
    p.add_argument("--audio", action="store_true", help="keep the clip's sound (off by default: a DVR's track is motors and bystanders)")
    p.add_argument("--poster", metavar="T", help="the frame at this video time as the page's poster and the preview image messengers show (none by default)")
    p.add_argument("--from", dest="start", help="the window's start in video time (instead of the moment minus --pad)")
    p.add_argument("--to", dest="end", help="the window's end in video time")
    p.add_argument("--update", metavar="TOKEN", help="edit the share with this token instead of making a new one: the bundle made again, the link kept")
    p.set_defaults(f=cmd_share)
    p = sp.add_parser("unshare", help="take a share down: its bundle removed (and the revoke command run, when set), the link dead")
    p.add_argument("token"); p.set_defaults(f=cmd_unshare)
    p = sp.add_parser("shares", help="the shares that are up; --all lists the expired and revoked ones too")
    p.add_argument("--all", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_shares)
