"""One verified .tar.gz of a whole directory: how a finished run's work tree is kept (#25).

The archive is written beside its destination as .partial, read back in full, and renamed only when every
regular file's (relative path, size) matches the source; gzip's CRC covers the content. Standard library
only (the images carry no zstd); the runs migrated on 2026-10-02 are .tar.zst (ops/pack-to-oak.py).
Restore: mkdir <dir> && tar -xzf <archive> -C <dir>.
"""
import os
import tarfile
from pathlib import Path


def inventory(root) -> dict[str, int]:
    found = {}
    for top, dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(top, name)
            if not os.path.islink(path):
                found[os.path.relpath(path, root)] = os.path.getsize(path)
    return found


def listing(archive) -> dict[str, int]:
    got = {}
    with tarfile.open(archive, mode="r|gz") as members:
        for m in members:
            if m.isreg():
                got[os.path.normpath(m.name)] = m.size
            elif m.islnk():  # a hard link carries no size of its own
                got[os.path.normpath(m.name)] = got[os.path.normpath(m.linkname)]
    return got


def pack(source, out) -> dict:
    """Archive `source` into `out` (.tar.gz); an existing `out` is kept as is."""
    source, out = Path(source), Path(out)
    if out.exists():
        return dict(archive=str(out), state="exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    with tarfile.open(partial, mode="w:gz", compresslevel=6, format=tarfile.PAX_FORMAT) as tar:
        tar.add(source, arcname=".")
    want, got = inventory(source), listing(partial)
    if want != got:
        missing = sorted(set(want) - set(got))[:5]
        differ = sorted(k for k in set(want) & set(got) if want[k] != got[k])[:5]
        raise ValueError(f"archive of {source} differs: missing {missing} size {differ}")
    os.replace(partial, out)
    return dict(archive=str(out), state="packed", files=len(want), bytes=sum(want.values()), packed=out.stat().st_size)
