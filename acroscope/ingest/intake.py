"""Taking a batch out of the inbox: the inbox is hostile ground (the push user owns everything in a batch), so only
regular files with plain names are read or moved, a sidecar and a manifest are read without following a link, and a
file is taken by a rename that cannot be swapped underneath.
"""
import json
import os
import re
from pathlib import Path


CLIP_RE = re.compile(r"^vid.*\.(mov|mp4|avi)$", re.I)


def plain_name(name: str) -> str:
    """A file name as the push user may give one: one path component, nothing hidden behind it."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name or name != os.path.basename(name):
        raise ValueError(f"{name!r}: not a plain file name")
    return name


def _read_nofollow(p: Path, limit: int = 1 << 20) -> str:
    """The text of a small file, opened without following a link (a link there fails the batch)."""
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise ValueError(f"{p.name}: {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if not os.path.isfile(p) or st.st_size > limit:
            raise ValueError(f"{p.name}: not a small regular file")
        return f.read().decode("utf-8", "replace")


def batch_entries(d: Path) -> list[Path]:
    """The regular files of a batch dir. Anything else there (a link, a directory, a file with several links, a
    name that is not plain) fails the batch: the push user put it there, and this runs as root."""
    out = []
    with os.scandir(d) as it:
        for e in it:
            plain_name(e.name)
            st = os.lstat(e.path)
            import stat as _st
            if not _st.S_ISREG(st.st_mode):
                raise ValueError(f"{e.name}: not a regular file")
            if st.st_nlink > 1:
                raise ValueError(f"{e.name}: has {st.st_nlink} links")
            out.append(Path(e.path))
    return sorted(out)


def sidecar(d: Path) -> dict:
    p = d / "import.json"
    if not os.path.lexists(p):
        return {}
    try:
        sc = json.loads(_read_nofollow(p))
    except json.JSONDecodeError as e:
        raise ValueError(f"import.json: {e}") from None
    if not isinstance(sc, dict):
        raise ValueError("import.json: not an object")
    return {k: v for k, v in sc.items() if isinstance(v, str)}


def manifest(d: Path) -> dict:
    p = d / ".done"
    if not os.path.lexists(p):
        raise ValueError("no .done marker: the batch is still arriving")
    try:
        txt = _read_nofollow(p).strip()
        man = json.loads(txt) if txt else {}
    except json.JSONDecodeError as e:
        raise ValueError(f".done: {e}") from None
    if not isinstance(man, dict) or not isinstance(man.get("files", {}), dict):
        raise ValueError(".done: not a manifest")
    for name in man.get("files") or {}:
        plain_name(name)
    return man


def check_sizes(d: Path, man: dict, taken: set[str] = frozenset()) -> None:
    """The manifest against the batch dir; `taken` are clips an earlier attempt already took out of it."""
    for name, size in (man.get("files") or {}).items():
        if plain_name(name) in taken and not os.path.lexists(d / name):
            continue
        f = d / name
        if not os.path.lexists(f):
            raise ValueError(f"{name}: in the manifest, not in the batch")
        st = os.lstat(f)
        if size is not None and st.st_size != size:
            raise ValueError(f"{name}: {st.st_size} bytes, the manifest says {size}")


def _take(src: Path, dst: Path) -> Path:
    """Move a batch file into a root-owned place, then make sure it is still a plain file there (the push user
    could have swapped it for a link between the look and the move), and give it to root."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)                                   # same filesystem; renames a link as a link, never follows
    st = os.lstat(dst)
    import stat as _st
    if not _st.S_ISREG(st.st_mode) or st.st_nlink > 1:
        dst.unlink()
        raise ValueError(f"{src.name}: was swapped for a link or hard-linked while being taken")
    if os.geteuid() == 0:
        os.chown(dst, 0, 0)
    os.chmod(dst, 0o644)
    return dst
