"""The workspace's secrets are not readable by other users on the host.

metadata.db is not a cache. It holds unexpired session tokens, in-flight PKCE
verifiers, scrypt password hashes and every connector DSN in the clear.
Everything here asserts the *mode on disk*, read back with os.stat, because
that is the only thing an attacker with a shell on the box cares about.

The policy under test is deliberately asymmetric, and the asymmetry is the
interesting part:

- files Laurelin creates are 0600 from the syscall that creates them;
- a database Laurelin *inherits* loses world access silently, because no
  working deployment can be reaching a read-write database through the
  ``other`` bits;
- an inherited database keeps its *group* access and complains, because a
  group is a set of principals somebody had to provision.

See laurelin/core/fileperms.py for the argument in full.
"""

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from laurelin.api import create_app, create_server_app
from laurelin.core import fileperms
from laurelin.core.auth import AuthService
from laurelin.core.backend import SQLiteBackend
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore

PG_URL = os.environ.get("LAURELIN_TEST_POSTGRES")


def mode(path: Path | str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture()
def ws(tmp_path):
    return Workspace.init(tmp_path / "ws", name="perm")


@pytest.fixture(autouse=True)
def _default_umask():
    """A permissive umask, so a passing test proves the *mode we asked for* and
    not a mode the environment happened to impose. 0o022 is the stock Linux
    default and is what produced the measured 0644 in the first place."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


@pytest.fixture(autouse=True)
def _not_strict(monkeypatch):
    monkeypatch.delenv(fileperms.STRICT_ENV, raising=False)


# --------------------------------------------------------------- on creation

def test_a_new_metadata_database_is_not_readable_by_other_users(ws):
    MetadataStore(ws.metadata_path)
    assert mode(ws.metadata_path) == 0o600


def test_a_new_metadata_database_holding_a_session_token_is_still_0600(ws):
    """The regression as measured: the exposure is the token, not the file."""
    store = MetadataStore(ws.metadata_path)
    auth = AuthService(store)
    user = auth.create_user("alice", "correct-horse-battery", role="admin")
    token, _ = auth.login(user)
    assert token  # a live bearer credential now sits in this file
    assert mode(ws.metadata_path) == 0o600


def test_the_workspace_marker_is_not_readable_by_other_users(ws):
    assert mode(ws.marker_path) == 0o600


def test_a_workspace_directory_laurelin_creates_is_owner_only(ws):
    assert mode(ws.root) == 0o700


def test_the_control_database_is_not_readable_by_other_users(tmp_path):
    """Multi-workspace mode: control.db holds *global* identity — every session
    and API token on the server, not one workspace's."""
    root = tmp_path / "srv"
    create_server_app(root)
    assert mode(root / "control.db") == 0o600
    assert mode(root) == 0o700


def test_the_sqlite_sidecars_are_as_private_as_the_database(ws):
    """-wal and -shm hold the same rows, and therefore the same secrets.

    Measured: SQLite creates both by copying the main database file's mode, so
    getting metadata.db right gets the siblings right and chasing the siblings
    separately would fix the copies while leaving the original. This test is
    what keeps that reasoning honest if SQLite ever changes it.
    """
    store = MetadataStore(ws.metadata_path)
    # Held open on purpose: SQLite checkpoints and unlinks -wal/-shm when the
    # last connection closes, so without a reader there is nothing to stat.
    conn = store.backend.connect()
    try:
        store.log_audit("session_created", {"token": "not-really"}, actor="alice")
        wal = ws.metadata_path.with_name(ws.metadata_path.name + "-wal")
        shm = ws.metadata_path.with_name(ws.metadata_path.name + "-shm")
        assert wal.exists() and shm.exists(), "expected a live WAL to inspect"
        assert mode(wal) == 0o600
        assert mode(shm) == 0o600
    finally:
        conn.close()


def test_the_database_is_private_without_any_chmod_at_all(ws, monkeypatch):
    """Proves the mode comes from creation, not from a later repair.

    os.chmod after open() leaves a window in which the file is 0644 — and
    SQLite writes its header, its schema and the first row inside that window.
    Worse, an attacker who opened an fd during it keeps reading through the
    chmod, because permission is checked at open and never again. So chmod is
    sabotaged here: if the fix depended on it, this test fails.
    """
    def no_chmod(*args, **kwargs):
        raise AssertionError("creation must not depend on chmod")

    monkeypatch.setattr(os, "chmod", no_chmod)
    MetadataStore(ws.root / "fresh.db")
    assert mode(ws.root / "fresh.db") == 0o600


def test_an_unopenable_database_still_fails_the_way_sqlite_fails(tmp_path):
    """Pre-creating must not become a second, worse place to report "cannot
    open database". A missing parent raised sqlite3.OperationalError before
    this change and has to keep doing so."""
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        MetadataStore(tmp_path / "no" / "such" / "dir" / "metadata.db")


def test_creating_a_private_file_never_truncates_an_existing_one(tmp_path):
    """O_EXCL, not O_TRUNC: this runs on every open of a database with data."""
    path = tmp_path / "already.db"
    path.write_bytes(b"precious")
    assert fileperms.create_private(path) is False
    assert path.read_bytes() == b"precious"


# ------------------------------------------------------- inherited workspaces

def test_a_world_readable_database_loses_world_access_when_it_is_opened(ws):
    """Every workspace that predates this fix is 0644. Opening one repairs it.

    Silent, and safe to be silent: 0644 grants read to others and write to
    nobody but the owner, while every process that opens this database opens it
    read-write. No component that currently works is reaching it through the
    ``other`` bits, so there is no configuration to break here — only an
    exposure to close.
    """
    MetadataStore(ws.metadata_path)
    os.chmod(ws.metadata_path, 0o644)  # what every older release produced

    store = MetadataStore(ws.metadata_path)

    assert mode(ws.metadata_path) == 0o640
    assert not (mode(ws.metadata_path) & fileperms.OTHER_BITS)
    assert store.backend.permission_note is not None  # group survived; say so


def test_a_group_readable_database_keeps_its_group_and_says_so(ws):
    """A group is a set of principals somebody provisioned — a backup agent, an
    on-call operator with read but not write. Stripping it on upgrade would
    turn a security fix into a silently broken backup, which is worse than the
    bug being fixed because nobody notices it. So it survives, loudly."""
    MetadataStore(ws.metadata_path)
    os.chmod(ws.metadata_path, 0o640)

    store = MetadataStore(ws.metadata_path)

    assert mode(ws.metadata_path) == 0o640
    note = store.backend.permission_note
    assert note and "chmod 600" in note


def test_strict_mode_strips_group_access_too(ws, monkeypatch):
    monkeypatch.setenv(fileperms.STRICT_ENV, "1")
    MetadataStore(ws.metadata_path)
    os.chmod(ws.metadata_path, 0o664)

    store = MetadataStore(ws.metadata_path)

    assert mode(ws.metadata_path) == 0o600
    assert store.backend.permission_note is None


def test_an_already_private_database_is_left_completely_alone(ws):
    MetadataStore(ws.metadata_path)
    os.chmod(ws.metadata_path, 0o600)

    store = MetadataStore(ws.metadata_path)

    assert mode(ws.metadata_path) == 0o600
    assert store.backend.permission_note is None


def test_a_workspace_directory_we_did_not_create_keeps_its_mode(tmp_path):
    """Never retro-tighten a directory.

    ``exist_ok`` already means the mode is only applied on creation, and that
    is the intended behaviour rather than an accident of the API: a directory
    the operator made is a directory whose mode the operator chose, the
    directory is not itself the secret, and narrowing a tree to 0700 reaches
    backup agents and log shippers that a single file's mode never touches.
    """
    root = tmp_path / "preexisting"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)

    ws = Workspace.init(root, name="inherited")
    MetadataStore(ws.metadata_path)

    assert mode(root) == 0o755
    # The directory is left as found; the secrets inside it are not.
    assert mode(ws.metadata_path) == 0o600
    assert mode(ws.marker_path) == 0o600


def test_a_database_we_are_not_allowed_to_chmod_is_reported_not_fatal(ws, monkeypatch):
    """Root created the file, the service runs as someone else.

    Refusing to start is a real option for a governance product, but it is not
    one earned by a defect this product shipped: the operator never chose 0644,
    Laurelin did. So the failure is reported and the server serves.
    """
    MetadataStore(ws.metadata_path)
    os.chmod(ws.metadata_path, 0o644)
    real_chmod = os.chmod

    def refuse(path, *args, **kwargs):
        if str(path) == str(ws.metadata_path):
            raise PermissionError(1, "Operation not permitted")
        return real_chmod(path, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", refuse)

    store = MetadataStore(ws.metadata_path)

    assert store.backend.permission_note is not None
    assert "chmod 600" in store.backend.permission_note
    assert store.count_users() == 0  # and it still works


def test_the_iceberg_catalog_database_is_private_too(ws):
    """The other database Laurelin causes to exist in a workspace root.

    Not the metadata store, and it holds no session token — but SQLAlchemy
    creates it at ``0666 & ~umask`` exactly as sqlite3 did, and a warehouse URI
    recorded inside it can carry a credential.
    """
    from laurelin.core import iceberg

    if not iceberg.available():
        pytest.skip("needs pyiceberg: pip install 'laurelin[iceberg]'")
    tables = iceberg.IcebergTables(ws)
    tables.catalog  # noqa: B018 - the property is what creates the file

    catalog_file = ws.root / "iceberg-catalog.db"
    assert catalog_file.exists()
    assert mode(catalog_file) == 0o600


# ------------------------------------------------------------- no local file

def test_an_in_memory_store_never_creates_a_file_called_memory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backend = SQLiteBackend(":memory:")
    assert backend.is_file_backed is False
    assert not (tmp_path / ":memory:").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not PG_URL, reason="set LAURELIN_TEST_POSTGRES to run")
def test_a_postgres_store_claims_no_local_file_and_creates_none(tmp_path, monkeypatch):
    """PostgreSQL keeps nothing on this host, and the report must say so rather
    than print a reassuring mode for a file that does not exist."""
    monkeypatch.chdir(tmp_path)
    store = MetadataStore(PG_URL)
    assert store.backend.is_file_backed is False
    assert store.backend.permission_note is None
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- admin API

def _client(ws) -> TestClient:
    return TestClient(create_app(ws, no_auth=True))


def test_the_admin_report_shows_the_mode_the_file_actually_has(ws):
    MetadataStore(ws.metadata_path)
    os.chmod(ws.metadata_path, 0o644)
    client = _client(ws)

    body = client.get("/api/v1/workspace/file-security").json()

    by_name = {f["name"]: f for f in body["files"]}
    # create_app opened the store, which repaired the world bits on the way in.
    assert by_name["metadata.db"]["mode"] == "0640"
    assert by_name["metadata.db"]["world_accessible"] is False
    assert by_name["metadata.db"]["group_accessible"] is True
    assert body["note"] and "chmod 600" in body["note"]


def test_the_admin_report_calls_a_private_workspace_private(ws):
    body = _client(ws).get("/api/v1/workspace/file-security").json()
    assert body["file_backed"] is True
    assert body["store_is_remote"] is False
    assert body["directory"]["mode"] == "0700"
    assert body["note"] is None
    modes = {f["name"]: f["mode"] for f in body["files"]}
    assert modes["metadata.db"] == "0600"
    assert modes["laurelin.yml"] == "0600"


def test_the_structured_report_carries_file_names_and_not_the_store_path(tmp_path):
    """The store's "path" on PostgreSQL is a DSN with a password in it. No
    field here should be one refactor away from printing it, so the entries
    carry basenames and nothing else."""
    ws = Workspace.init(tmp_path / "supersecret-root", name="perm")
    body = _client(ws).get("/api/v1/workspace/file-security").json()

    assert body["note"] is None  # nothing to complain about here
    for entry in [body["directory"], *body["files"]]:
        assert "/" not in entry["name"]
        assert "supersecret-root" not in str(entry)


def test_the_advisory_note_can_never_be_a_dsn(tmp_path):
    """``note`` does name a local file — an operator cannot chmod a basename,
    and the workspace root is not a secret (``GET /workspace`` hands it to
    every viewer). What it must never be is a connection string, and it cannot
    be: only the SQLite backend ever produces one."""
    ws = Workspace.init(tmp_path / "ws", name="perm")
    MetadataStore(ws.metadata_path)
    os.chmod(ws.metadata_path, 0o644)

    body = _client(ws).get("/api/v1/workspace/file-security").json()

    assert body["note"] and "metadata.db" in body["note"]
    assert "://" not in body["note"]


def test_the_advisory_stops_once_the_operator_has_acted(ws):
    """A warning that outlives its cause teaches operators to ignore warnings.

    The note is written once, when the store is opened; the modes are stat'd
    per request. An admin who runs the chmod the note asked for must not be
    nagged by a stale sentence sitting beside a table that already reads 0600.
    """
    MetadataStore(ws.metadata_path)
    os.chmod(ws.metadata_path, 0o644)
    client = _client(ws)  # opens the store, strips world, records the note
    assert client.get("/api/v1/workspace/file-security").json()["note"]

    os.chmod(ws.metadata_path, 0o600)  # the operator takes the advice

    body = client.get("/api/v1/workspace/file-security").json()
    assert body["note"] is None
    assert {f["name"]: f["mode"] for f in body["files"]}["metadata.db"] == "0600"


def test_a_viewer_cannot_read_the_file_security_report(ws):
    """Where the credentials live is a fact an attacker wants; it is admin-only."""
    store = MetadataStore(ws.metadata_path)
    auth = AuthService(store)
    auth.create_user("vera", "correct-horse-battery", role="viewer")
    client = TestClient(create_app(ws))
    client.post("/api/v1/auth/login", json={"username": "vera",
                                            "password": "correct-horse-battery"})

    assert client.get("/api/v1/workspace/file-security").status_code == 403


# ------------------------------------------------------------------- the UI

def test_the_built_ui_ships_the_file_permissions_panel():
    """Reachable through the UI, not only the API — the whole reason the
    partial repair is defensible is that the operator can see the residual."""
    bundle = Path(__file__).resolve().parents[1] / "laurelin/ui/static/index.html"
    text = bundle.read_text(encoding="utf-8")
    assert "Workspace files on disk" in text
    assert "LAURELIN_STRICT_FILE_MODE" in text


# ------------------------------------------------- second round: what was found
#
# The module above shipped and was then attacked. These are its holes.

def test_the_note_reports_the_mode_the_file_has_and_not_the_one_we_asked_for(
    tmp_path, monkeypatch
):
    """``harden_existing`` called ``chmod`` and then reasoned about ``target``
    — the mode it *requested* — for both the log line and the note, never
    re-stating the file.

    On a filesystem that accepts chmod and ignores it (vfat, exfat, ntfs-3g,
    several CIFS mounts, FUSE object-store gateways — "the workspace is on a
    mount that ignores chmod" is an ordinary deployment, not a contrivance)
    that made the note assert a tightening that had not happened. It is the
    field SECURITY.md and docs/DEPLOYMENT.md tell the operator to act on.
    """
    db = tmp_path / "metadata.db"
    db.write_text("x")
    os.chmod(db, 0o644)
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)

    note = fileperms.harden_existing(db, what="metadata database")

    assert mode(db) == 0o644, "precondition: the stub really did ignore the chmod"
    assert note is not None
    assert "0644" in note
    assert "World access was removed" not in note
    assert "0640" not in note


def test_a_file_that_was_really_tightened_still_reports_the_group_residual(tmp_path):
    """The other side of the same change: when chmod works, the note must still
    be the one the partial-repair argument in this module's docstring
    promises."""
    db = tmp_path / "metadata.db"
    db.write_text("x")
    os.chmod(db, 0o644)

    note = fileperms.harden_existing(db, what="metadata database")

    assert mode(db) == 0o640
    assert note is not None and "0640" in note and "group" in note


def test_the_admin_report_names_the_control_database(tmp_path):
    """``control.db`` holds every user, session token and API token for *every*
    workspace on the server, and the report was built from the per-workspace
    store alone — so on an upgraded deployment the one file with the widest
    blast radius got a WARNING in a log and nothing else. This module's own
    argument for the panel is that a log nobody tails is not a decision anybody
    makes."""
    root = tmp_path / "srv"
    root.mkdir(mode=0o755)
    control = root / "control.db"
    control.touch()
    os.chmod(control, 0o644)

    app = create_server_app(root, no_auth=True)
    client = TestClient(app)
    client.post("/api/v1/workspaces", json={"slug": "team-a", "name": "A"})
    body = client.get(
        "/api/v1/workspace/file-security", headers={"X-Laurelin-Workspace": "team-a"}
    ).json()

    reported = {entry["name"]: entry for entry in body["files"]}
    assert "control.db" in reported, reported
    assert reported["control.db"]["mode"] == "0640"
    assert reported["control.db"]["group_accessible"] is True
    # And the residual is surfaced, not just listed.
    assert body["note"] and "control.db" in body["note"]


def test_the_workspace_subdirectories_are_private_under_a_root_we_did_not_create(
    tmp_path, monkeypatch
):
    """``Workspace.init`` hardened the root and then created ``data/``,
    ``pipelines/`` and ``ontology/`` with a bare ``mkdir`` — mode
    ``0777 & ~umask``.

    Inside a Laurelin-made 0700 root that is invisible, which is why it
    survived the suite. It becomes the real mode the moment the root
    pre-exists: an upgraded workspace, or the shipped Dockerfile's
    ``RUN mkdir -p /data``. Measured at umask 022, every dataset Parquet was
    0644 inside 0755 directories — every governed dataset readable by any local
    user with no row policy and no column masks. At umask 000 ``pipelines/``
    came out 0777, and ``transforms.api.collect_transforms`` exec()s what it
    finds there.
    """
    root = tmp_path / "provisioned"
    root.mkdir(mode=0o755)
    monkeypatch.setattr(os, "umask", lambda _: 0)
    os.umask(0)

    workspace = Workspace.init(root, name="prov")

    for directory in (workspace.data_dir, workspace.pipelines_dir,
                      workspace.ontology_dir):
        assert mode(directory) == 0o700, f"{directory} is {mode(directory):04o}"
        assert not mode(directory) & 0o002, "a directory whose contents get exec'd"


def test_the_import_state_file_is_never_visible_at_a_wider_mode(tmp_path):
    """``write_text`` then ``chmod`` is a window, and an out-of-process poller
    caught this one at 0644 with its content already on disk. Both call sites
    in ``export/reader.py`` had the shape that ``fileperms`` and
    ``export/writer.py`` both document as unacceptable, while ``cli.py`` already
    had the right primitive.

    Asserted by sabotaging ``os.chmod``: if the mode comes from creation, no
    chmod is needed and removing it changes nothing.
    """
    target = tmp_path / ".laurelin-import.json"

    def refuse(*args, **kwargs):
        raise AssertionError("write_private must not need a chmod")

    original = os.chmod
    os.chmod = refuse
    try:
        fileperms.write_private(target, '{"import_state": "acknowledged"}')
    finally:
        os.chmod = original

    assert mode(target) == 0o600
    assert target.read_text() == '{"import_state": "acknowledged"}'
    # And it replaces an existing file without ever widening it.
    fileperms.write_private(target, '{"import_state": "x"}')
    assert mode(target) == 0o600


def test_acknowledging_an_import_never_widens_the_state_file(ws):
    """The call site, not just the primitive.

    ``reader.acknowledge_pipelines`` and ``reader._write_import_state`` both
    did ``write_text`` then ``chmod``; testing only ``write_private`` would
    keep passing if either reverted. Same sabotage, so the assertion is that
    the mode comes from creation rather than from a repair.
    """
    from laurelin.export import reader

    state = ws.root / ".laurelin-import.json"
    fileperms.write_private(state, '{"import_state": "pipelines_unacknowledged"}')

    def refuse(*args, **kwargs):
        raise AssertionError("the import state file must not need a chmod")

    original = os.chmod
    os.chmod = refuse
    try:
        reader.acknowledge_pipelines(ws, actor="admin")
    finally:
        os.chmod = original

    assert mode(state) == 0o600
    assert reader.pipelines_acknowledged(ws) is True


def test_an_empty_database_path_is_not_a_file_and_never_chmods_a_directory(tmp_path):
    """``_NO_FILE`` lists ``""``, but the comparison ran on ``str(Path(path))``
    and ``str(Path("")) == "."``. So the empty string — one of the two values
    the guard exists to catch — resolved to the current working directory:
    ``is_file_backed`` said True, ``O_EXCL`` failed with EEXIST because the
    directory is already there, and ``harden_existing`` chmod'd it."""
    directory = tmp_path / "cwd"
    directory.mkdir(mode=0o755)
    previous = os.getcwd()
    os.chdir(directory)
    try:
        backend = SQLiteBackend("")
        assert backend.is_file_backed is False
        assert backend.permission_note is None
    finally:
        os.chdir(previous)
    assert mode(directory) == 0o755


# --------------------------------------------- third round: what was found next

def test_an_inherited_group_writable_database_loses_the_write_bit(ws):
    """``harden_existing`` computed ``current & ~OTHER_BITS`` and preserved the
    whole of ``0o070`` on the argument that a group may be a provisioned set of
    principals — an argument whose every sentence is about *read*.

    ``metadata.db`` is the file that says who is an admin. At ``umask 002``
    (the Debian/Ubuntu login default under ``USERGROUPS_ENAB``, and what a
    systemd unit with ``UMask=0002`` sets) a pre-``fileperms`` release left it
    0664; the repair took it to 0660 and called that "readable by its group" on
    the admin screen, in the note and in the UI badge. A member of that group,
    not a Laurelin user at all, opened it with ``sqlite3``, ran
    ``update users set role='admin'``, and reached the ADMIN-only routes.
    """
    MetadataStore(ws.metadata_path)
    for legacy, expected in ((0o664, 0o640), (0o660, 0o640), (0o666, 0o640),
                             (0o620, 0o600), (0o640, 0o640)):
        os.chmod(ws.metadata_path, legacy)
        store = MetadataStore(ws.metadata_path)
        assert mode(ws.metadata_path) == expected, f"{legacy:04o}"
        assert not mode(ws.metadata_path) & fileperms.GROUP_WRITE
        # Group *read* still survives, which is the case the residual is for.
        if expected & fileperms.GROUP_BITS:
            note = store.backend.permission_note
            assert note and "chmod 600" in note


def test_the_admin_report_distinguishes_group_write_from_group_read(ws):
    """One ``group_accessible`` flag covering r, w and x is what let a 0660
    database be rendered as "readable by its group". The screen has to be able
    to say which grant it is looking at."""
    MetadataStore(ws.metadata_path)
    os.chmod(ws.metadata_path, 0o640)
    assert fileperms.describe(ws.metadata_path)["group_writable"] is False
    os.chmod(ws.metadata_path, 0o660)
    described = fileperms.describe(ws.metadata_path)
    assert described["group_writable"] is True
    assert described["group_accessible"] is True
    # And on a filesystem that accepts chmod and ignores it — vfat, several
    # CIFS mounts, FUSE object-store gateways — the note for a surviving write
    # bit does not say "readable". A group member with write on this file can
    # make themselves an admin, and the sentence has to say so.
    real = os.chmod
    try:
        os.chmod = lambda *a, **k: None
        note = fileperms.harden_existing(ws.metadata_path, what="metadata database")
    finally:
        os.chmod = real
    assert note and "WRITABLE" in note and "admin" in note


def test_a_relocation_symlink_gets_a_private_file_from_the_first_byte(tmp_path):
    """Relocating the database to another volume before first start —
    ``ln -s /mnt/fastvol/ws1.db <ws>/metadata.db`` — is ordinary, and the
    target does not exist yet.

    ``O_EXCL`` refuses to follow a symlink, so ``create_private`` failed with
    ``EEXIST``; ``harden_existing`` then stat'd through the dangling link, got
    ``-1``, and returned ``None`` having done nothing. sqlite3 followed the
    link and created the real file at ``0666 & ~umask`` — 0644, holding the
    scrypt hashes, session tokens and connector DSNs of the bootstrap run —
    with ``permission_note`` ``None``, so nothing on any surface said so. It
    self-repaired on the *next* start, i.e. the run after the one that
    mattered.
    """
    root = tmp_path / "ws"
    workspace = Workspace.init(root, name="relocated")
    volume = tmp_path / "fastvol"
    volume.mkdir()
    target = volume / "ws1.db"
    workspace.metadata_path.unlink(missing_ok=True)
    os.symlink(target, workspace.metadata_path)
    assert not target.exists()

    store = MetadataStore(workspace.metadata_path)
    AuthService(store).create_user("root", "trustno1!", role="admin")

    assert mode(target) == 0o600
    assert store.backend.permission_note is None
    assert fileperms.describe(workspace.metadata_path)["mode"] == "0600"


def test_strict_mode_reaches_the_marker_and_the_directory_it_names_on_screen(
    tmp_path, monkeypatch
):
    """``LAURELIN_STRICT_FILE_MODE=1`` is the remedy the admin screen and
    ``docs/DEPLOYMENT.md`` advertise, and on an inherited workspace it touched
    neither ``laurelin.yml`` nor the workspace directory — both of which that
    same screen renders in the same table, tagged red. The operator was left
    with two permanently red rows and two remedies that provably do nothing:
    ``chmod 600 metadata.db`` names a file already 0600, and the variable was
    already set.

    A restart is what the card asks for, and a restart calls ``find``, not
    ``init`` — so the repair has to run there too.
    """
    monkeypatch.setenv(fileperms.STRICT_ENV, "1")
    root = tmp_path / "inherited"
    root.mkdir(mode=0o755)
    for name in ("data", "pipelines", "ontology"):
        (root / name).mkdir(mode=0o755)
    (root / "laurelin.yml").write_text("name: legacy\ndescription: ''\n")
    os.chmod(root / "laurelin.yml", 0o644)
    os.chmod(root, 0o755)

    workspace = Workspace.find(root)  # the restart

    assert mode(root / "laurelin.yml") == 0o600
    assert mode(root) == 0o700
    assert mode(root / "data") == 0o700
    report = _client(workspace).get("/api/v1/workspace/file-security").json()
    assert not any(e["world_accessible"] for e in report["files"])
    assert report["directory"]["world_accessible"] is False


def test_the_iceberg_warehouse_is_private_under_a_root_we_did_not_create(tmp_path):
    """``data/``, ``pipelines/`` and ``ontology/`` are created 0700 because the
    workspace root is only 0700 when *Laurelin* made it, and the documented
    Docker shape (``RUN mkdir -p /data``, or any bind-mounted volume) leaves it
    0755. ``iceberg/`` arrived later and never joined that list: a bare
    ``mkdir`` gave it ``0777 & ~umask``, pyiceberg wrote its data and metadata
    at 0644 inside it, and the whole chain came out world-readable — governed
    dataset Parquet with no row policy and no column masks, reachable by every
    local user."""
    import pyarrow as pa

    from laurelin.catalog import DatasetCatalog
    from laurelin.core import iceberg

    if not iceberg.available():
        pytest.skip("needs pyiceberg: pip install 'laurelin[iceberg]'")

    root = tmp_path / "preexisting"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    workspace = Workspace.init(root, name="ice")
    store = MetadataStore(workspace.metadata_path)
    catalog = DatasetCatalog(workspace, store)
    catalog.write_iceberg("payroll", pa.table({"ssn": ["111-22-3333"]}))

    warehouse = root / "iceberg"
    assert warehouse.is_dir()
    assert mode(warehouse) == 0o700, "the whole tree below this is 0755/0644"
    # And the catalog database it sits beside is on the admin screen, which is
    # the only place the residual it deliberately leaves behind can be seen.
    report = _client(workspace).get("/api/v1/workspace/file-security").json()
    assert any(e["name"] == "iceberg-catalog.db" for e in report["files"]), report


def test_a_local_data_uri_does_not_take_the_data_plane_out_of_its_private_root(
    tmp_path, monkeypatch
):
    """``LAURELIN_DATA_URI`` pointing at a local path put every governed
    Parquet outside the 0700 workspace, in a tree built by a bare
    ``root.mkdir(parents=True)``: 0755 directories, 0644 files."""
    import pyarrow as pa

    from laurelin.catalog import DatasetCatalog
    from laurelin.core.storage import Storage

    shared = tmp_path / "shared"
    monkeypatch.setenv("LAURELIN_DATA_URI", str(shared))
    workspace = Workspace.init(tmp_path / "ws", name="w")
    store = MetadataStore(workspace.metadata_path)
    storage = Storage.for_workspace(workspace)
    DatasetCatalog(workspace, store, storage=storage).write(
        "payroll", pa.table({"ssn": ["111-22-3333"]})
    )
    assert mode(Path(storage.uri)) == 0o700


UNSUPPORTED_URIS = ["S3://bucket/prefix", "s3:/bucket/prefix", "s3a://bucket/p",
                    "wasb://c@a/p", "hdfs://nn/p"]


@pytest.mark.parametrize("uri", UNSUPPORTED_URIS)
def test_a_data_uri_this_build_cannot_address_is_refused_not_made_a_directory(
    tmp_path, monkeypatch, uri
):
    """``is_remote_uri`` was a case-sensitive exact-prefix match against a fixed
    tuple, so ``S3://``, ``s3:/`` and ``s3a://`` were all judged *local* and
    became directories literally named ``S3:/bucket/prefix`` under the process
    CWD — 0755, 0644 Parquet inside, on the container's ephemeral disk, while
    the operator believed the governed data was in a bucket behind a bucket
    policy. Nothing logged, warned or failed.

    ``S3://`` is now recognised as the object store it is; the rest fail
    loudly, because a misconfiguration that silently relocates the data plane
    is not one an operator can be expected to notice.
    """
    from laurelin.core.storage import Storage, UnsupportedDataURI, is_remote_uri

    monkeypatch.chdir(tmp_path)
    if uri == "S3://bucket/prefix":
        assert is_remote_uri(uri) is True
        return
    assert is_remote_uri(uri) is False
    with pytest.raises(UnsupportedDataURI) as caught:
        Storage.for_uri(uri)
    assert uri in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_two_processes_initialising_one_workspace_do_not_race_on_the_marker(
    tmp_path,
):
    """``if not ws.marker_path.exists(): ws._write_marker(...)`` is a
    check-then-``O_EXCL``-create, and 17 of 40 six-process trials had a loser
    die on an unhandled ``FileExistsError``. It is reachable from the front
    door: ``api/context._bundle`` calls ``Workspace.init`` lazily for a
    registered-but-unmaterialised workspace and FastAPI runs sync endpoints on
    a threadpool, so two simultaneous requests for one slug both enter and the
    second gets a 500 instead of a workspace. ``fileperms.create_private``
    handles the identical race by catching ``OSError``; this is the copy that
    did not."""
    from concurrent.futures import ThreadPoolExecutor

    root = tmp_path / "contended"
    barrier = __import__("threading").Barrier(8)

    def go():
        barrier.wait()
        return Workspace.init(root, name="shared").root

    with ThreadPoolExecutor(max_workers=8) as pool:
        # .result() re-raises; a FileExistsError in any thread fails here.
        roots = [f.result() for f in [pool.submit(go) for _ in range(8)]]
    assert roots == [root.resolve()] * 8
    assert mode(root / "laurelin.yml") == 0o600
    assert Workspace.find(root).name == "shared"
