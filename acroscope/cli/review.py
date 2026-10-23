"""Phase 4's commands: note, tag, untag, moments, the tag vocabulary (tags, tagdef, category), import-xspf.
"""
import sys

from ..analyse import metrics
from ..review import moments, xspf
from ..store import sessions
from .common import _arm_for, _session, fmt_time, out, parse_time
from ..config import TAGS


def cmd_note(a):
    moments.set_note(_session(a.session), a.text, a.clip)
    out({"ok": True})


def cmd_tags(a):
    st = sessions.store()
    t, use = st.tags(), st.tag_usage(_session(a.session) if a.session else None)
    if a.json:
        return out({**t, "usage": use})
    defined = {x["name"] for x in t["tags"]}
    for c in t["categories"]:
        print(f"{c['name']}  {c['color']}")
        for x in t["tags"]:
            if x["category"] == c["name"]:
                print(f"  {x['name']:12} {x['color'] or '':8} {use.get(x['name'], 0):3} moment(s)")
    loose = {k: v for k, v in use.items() if k not in defined}
    if loose:
        print("(no category)")
        for k, v in sorted(loose.items()):
            print(f"  {k:12} {'':8} {v:3} moment(s)")


def cmd_tagdef(a):
    st = sessions.store()
    if a.rm:
        return out({"removed": st.delete_tag(a.name)})
    if not a.category:
        sys.exit("tagdef: --category is needed (or --rm)")
    out(st.set_tag(a.name, a.category, a.color))


def cmd_category(a):
    st = sessions.store()
    if a.rm:
        return out({"removed": st.delete_category(a.name)})
    out(st.set_category(a.name, a.color))


def cmd_tag(a):
    s = _session(a.session)
    start, end = parse_time(a.start), parse_time(a.end)
    tags = [t.strip() for t in (a.tags or "").split(",") if t.strip()]
    snap = None
    if a.metrics:
        sd = sessions.load(s)
        m, arm = _arm_for(sd, sessions.resolve_clip(s, a.video), start)
        if arm:
            snap = metrics.summary(arm, start - m["offset"], end - m["offset"])
    out(moments.tag(s, a.video, start, end, a.title, tags, a.note or "", snap, a.id))


def cmd_untag(a):
    out({"removed": moments.untag(_session(a.session), a.id)})


def cmd_moments(a):
    s = sessions.load(_session(a.session))
    if a.json:
        return out(s["moments"])
    for m in s["moments"]:
        print(f"{m['id']:4} {m['video'][-7:-4]} {fmt_time(m['start'])}-{fmt_time(m['end'])}  {m['title']}  [{', '.join(m['tags'])}]" + (f"  | {m['note']}" if m.get("note") else ""))


def cmd_import_xspf(a):
    out(xspf.import_session(_session(a.session)))


def add(sp) -> None:
    """This group's subcommands."""
    p = sp.add_parser("note", help="set the note of a session, or of one clip")
    p.add_argument("session"); p.add_argument("text"); p.add_argument("--clip"); p.set_defaults(f=cmd_note)
    p = sp.add_parser("tags", help="the tag vocabulary by category, with how many moments use each tag")
    p.add_argument("session", nargs="?", help="count in one session only"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_tags)
    p = sp.add_parser("tagdef", help="define a tag in a category (or move it there); --rm drops the definition")
    p.add_argument("name"); p.add_argument("--category"); p.add_argument("--color", help="#rrggbb; '' clears it, the category colour is used then")
    p.add_argument("--rm", action="store_true"); p.set_defaults(f=cmd_tagdef)
    p = sp.add_parser("category", help="create a tag category (needs --color) or change its colour; --rm drops an empty one")
    p.add_argument("name"); p.add_argument("--color"); p.add_argument("--rm", action="store_true"); p.set_defaults(f=cmd_category)
    p = sp.add_parser("tag", help="add (or with --id replace) a moment")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("start"); p.add_argument("end"); p.add_argument("title")
    p.add_argument("--tags", help=f"comma-separated, e.g. {','.join(TAGS[:4])}"); p.add_argument("--note"); p.add_argument("--id")
    p.add_argument("--metrics", action="store_true", help="store a metrics snapshot (needs a match)"); p.set_defaults(f=cmd_tag)
    p = sp.add_parser("untag", help="remove a moment by id"); p.add_argument("session"); p.add_argument("id"); p.set_defaults(f=cmd_untag)
    p = sp.add_parser("moments", help="list a session's moments"); p.add_argument("session"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_moments)
    p = sp.add_parser("import-xspf", help="one-off: import videos/<session>/moments.xspf into the session file"); p.add_argument("session"); p.set_defaults(f=cmd_import_xspf)
