"""Filesystem permissions for the files that hold Laurelin's secrets.

``metadata.db`` is not a cache. It holds live session tokens, unexpired PKCE
verifiers, scrypt password hashes and every connector DSN in the clear. Until
this module existed nothing in the codebase ever passed a mode to ``open`` or
``mkdir``, so every one of those files was created at ``0666 & ~umask`` — 0644
on a stock Linux host. Any local user could read a bearer token and replay it.

Two rules, and they are deliberately different rules:

**Files Laurelin creates are private from the first byte.** Not created and
then chmod'd: ``os.open(..., O_CREAT | O_EXCL | O_WRONLY, 0o600)``. The window
between ``open()`` and ``chmod()`` is not theoretical for a database — SQLite
writes the header, the schema and the first session row inside it, and an
attacker who already holds an open fd keeps reading through the chmod, because
permissions are checked at open time and never again. ``umask`` cannot widen
the mode we ask for, only narrow it, so 0600 is an upper bound and a tighter
umask stays honoured.

**Files Laurelin inherits are only narrowed where the mode cannot have been
deliberate.** See :func:`harden_existing`.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Optional

log = logging.getLogger("laurelin.fileperms")

PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700

OTHER_BITS = 0o007
GROUP_BITS = 0o070

#: Set to "1" to also strip group access from files Laurelin did not create.
#: Off by default because a group *can* be a deliberately provisioned set of
#: principals; see :func:`harden_existing`.
STRICT_ENV = "LAURELIN_STRICT_FILE_MODE"


def strict_mode() -> bool:
    return os.environ.get(STRICT_ENV) == "1"


def mode_of(path: Path | str) -> int:
    """Permission bits of `path`, or -1 if it is not there."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return -1


def create_private(path: Path | str) -> bool:
    """Create `path` as an empty 0600 file. Returns False if it was not created.

    Never truncates: ``O_EXCL`` means an existing file is left exactly as it
    is, which is what makes this safe to call on every open of a database that
    may already hold data.

    Any *other* OSError — missing parent, unwritable directory, a directory
    where a file was expected — is swallowed rather than raised. Not because it
    does not matter, but because the caller is about to open the same path with
    sqlite3, which will fail for the identical reason and say so in the words
    this codebase's tests and users already know. Pre-creating must not become
    a second, worse place for "cannot open database" to be reported.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE)
    except OSError:
        return False
    os.close(fd)
    return True


def harden_existing(path: Path | str, *, what: str = "file") -> Optional[str]:
    """Narrow the mode of a file Laurelin did not create. Returns a note about
    what is still wrong, or None when the file ends up private.

    **World access is removed, always, without asking.** It cannot be a working
    deployment's intent: 0644 grants *read* to others but write to nobody but
    the owner, and every process that opens this database opens it read-write.
    So no component that currently functions is reaching the file through the
    ``other`` bits — there is no configuration to break, only an exposure to
    close. And "others" on a shared host is precisely the unbounded set this
    task is about.

    **Group access is preserved, and complained about.** The same argument does
    not hold: a group is a bounded, named set of principals that somebody had
    to provision, and a group-readable database is exactly how a backup agent
    or an on-call operator legitimately reads one without write access. Ripping
    that out on upgrade would turn a security fix into a silently broken
    backup, which is a worse failure than the one being fixed because nobody
    notices it. So the group bits survive and the operator is told, in one line
    with the command to finish the job. ``LAURELIN_STRICT_FILE_MODE=1`` strips
    them too, for deployments that want 0600 exactly.

    A chmod we are not permitted to make (root created the file, the service
    runs as someone else) is reported, never raised: refusing to start is a
    real option for a governance product, but not one earned by a defect the
    product itself shipped.
    """
    current = mode_of(path)
    if current < 0:
        return None
    target = current & ~OTHER_BITS
    if strict_mode():
        target &= ~GROUP_BITS
    if target != current:
        try:
            os.chmod(path, target)
        except OSError as exc:
            note = (
                f"{what} {path} is mode {current:04o} and could not be "
                f"tightened ({exc.strerror or exc}). It holds session tokens "
                f"and connector credentials. Fix with: chmod 600 {path}"
            )
            log.warning(note)
            return note
    # Re-stat, and reason only about what the file *has* from here on.
    #
    # This function used to carry `target` — the mode it asked for — into both
    # the log line and the note. On any filesystem that accepts chmod and
    # ignores it (vfat, exfat, ntfs-3g, several CIFS mounts and FUSE
    # object-store gateways: "a workspace on a mount that ignores chmod" is not
    # a hypothetical) that made both of them false. Measured with os.chmod
    # stubbed to a no-op: a file still 0644 on disk produced "is mode 0640 ...
    # World access was removed." `permission_note` is the field SECURITY.md and
    # docs/DEPLOYMENT.md tell the operator to act on, so it stated the opposite
    # of the truth about the one thing it exists to report.
    actual = mode_of(path)
    if actual < 0:
        return None
    if actual != current:
        log.info(
            "tightened %s from %04o to %04o", path, current, actual,
            extra={"path": str(path), "was": f"{current:04o}", "now": f"{actual:04o}"},
        )
    if actual & OTHER_BITS:
        note = (
            f"{what} {path} is mode {actual:04o}: readable by every user on "
            f"this host, and the chmod meant to fix that did not take (a mount "
            f"that ignores permissions?). It holds session tokens and connector "
            f"credentials. Move the workspace to a filesystem that enforces "
            f"modes, or run: chmod 600 {path}"
        )
        log.warning(note)
        return note
    if actual & GROUP_BITS:
        note = (
            f"{what} {path} is mode {actual:04o}: readable by its group. World "
            f"access was removed. If the group is not a set of principals you "
            f"chose, run: chmod 600 {path}"
        )
        log.warning(note)
        return note
    return None


def ensure_private_file(path: Path | str, *, what: str = "file") -> Optional[str]:
    """Create `path` at 0600, or narrow it if it is already there.

    The single entry point for a credential-bearing file, so the create path
    and the inherited path can never drift apart.
    """
    if create_private(path):
        return None
    return harden_existing(path, what=what)


def write_private(path: Path | str, text: str) -> None:
    """Write `text` to `path` so it is never visible at a wider mode.

    ``write_text`` then ``chmod`` leaves a real window, not a theoretical one:
    an out-of-process poller caught ``.laurelin-import.json`` at 0644 with its
    content already on disk, and wrapping ``os.chmod`` to stat immediately
    before it fires showed 0644 with 66 bytes written. ``os.replace`` is atomic
    within a filesystem and carries the temporary file's mode to the
    destination, so a reader sees either the old file or the new one, never a
    world-readable new one.

    The temporary name is dotted and suffixed so a partial write is obvious in
    a directory listing and cannot be mistaken for the real file by anything
    globbing for ``*.json``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.partial")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def mkdir_private(path: Path | str) -> None:
    """Create `path` (and any missing parents) with the leaf at 0700.

    Only the leaf: ``Path.mkdir(parents=True)`` gives intermediate directories
    the default mode, which is right — Laurelin owns the workspace directory,
    not ``/srv``.

    An existing directory is left alone. ``exist_ok=True`` already ignores the
    mode, and that is the intended behaviour rather than an accident of the
    API: a directory the operator made is a directory whose mode the operator
    chose, and a directory is not itself a secret — the secret inside it is a
    file this module has already made 0600. Retro-tightening the tree to 0700
    has a blast radius (backup agents, log shippers, a sibling process reading
    ``data/``) out of all proportion to what it adds on top of a private
    database file.
    """
    Path(path).mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=True)


def describe(path: Path | str, name: Optional[str] = None) -> dict:
    """A reportable view of one path's mode.

    Reports the *name* and never the absolute path: the admin API renders this,
    and the store's "path" on a PostgreSQL deployment is a DSN with a password
    in it. Nothing here should ever be one refactor away from printing that.
    """
    mode = mode_of(path)
    return {
        "name": name or Path(path).name,
        "exists": mode >= 0,
        "mode": f"{mode:04o}" if mode >= 0 else None,
        "world_accessible": mode >= 0 and bool(mode & OTHER_BITS),
        "group_accessible": mode >= 0 and bool(mode & GROUP_BITS),
    }


__all__ = [
    "GROUP_BITS",
    "OTHER_BITS",
    "PRIVATE_DIR",
    "PRIVATE_FILE",
    "STRICT_ENV",
    "create_private",
    "describe",
    "ensure_private_file",
    "harden_existing",
    "mkdir_private",
    "mode_of",
    "strict_mode",
]
