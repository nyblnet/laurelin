"""The archive itself: its shape, its permissions, and what it costs to write.

Nothing here asserts on governance — that is
tests/test_export_governance_roundtrip.py. These are the properties an operator
checks with ``tar`` and ``ls -l`` before they trust the thing at all.
"""

import io
import json
import os
import resource
import subprocess
import tarfile

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.export import (
    ExportOptions,
    ExportRefused,
    ExportTrailer,
    export_workspace,
    preview_manifest,
    stream_export,
)

HARMLESS_PIPELINE = (
    "from laurelin.transforms import transform, Input, Output\n"
    "@transform(output=Output('clean'), s=Input('sales'))\n"
    "def clean(s):\n    return s\n"
)


@pytest.fixture()
def ws(tmp_path):
    workspace = Workspace.init(tmp_path / "src", name="Acme Production")
    store = MetadataStore(workspace.metadata_path)
    catalog = DatasetCatalog(workspace, store)
    catalog.write("sales", pa.table({"region": ["eu", "us"], "amount": [1, 2]}))
    (workspace.ontology_dir / "o.yml").write_text(
        "object_types:\n"
        "  - api_name: sale\n"
        "    backing_dataset: sales\n"
        "    primary_key: region\n"
        "    properties:\n"
        "      region: {type: string}\n"
    )
    (workspace.pipelines_dir / "p.py").write_text(HARMLESS_PIPELINE)
    return workspace


@pytest.fixture()
def store(ws):
    return MetadataStore(ws.metadata_path)


class _Unseekable(io.RawIOBase):
    """A sink that behaves like a pipe: no seek, no tell, write-only."""

    def __init__(self):
        self.buffer = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.buffer += bytes(data)
        return len(data)

    def seekable(self) -> bool:
        return False

    def seek(self, *args):
        raise OSError("this sink is a pipe; seeking it is the bug under test")

    def tell(self):
        raise OSError("this sink is a pipe; telling it is the bug under test")


def _members(raw: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|*") as tar:
        return [m.name for m in tar]


def _member_bytes(raw: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|*") as tar:
        for member in tar:
            if member.name == name:
                return tar.extractfile(member).read()
    raise AssertionError(f"no member {name!r}")


def test_the_archive_streams_without_a_seekable_output(ws, store):
    """`laurelin export - | ssh host laurelin import -` is the whole reason the
    format is tar rather than zip, and a single seek would break it."""
    sink = _Unseekable()
    stream_export(ws, store, sink, ExportOptions())
    assert b"manifest.json" in bytes(sink.buffer)


def test_the_manifest_is_the_first_member_and_the_trailer_is_the_last(ws, store):
    """Member order is normative: a streaming importer reads intent from the
    head and verification from the tail, and can do neither if they move."""
    sink = _Unseekable()
    stream_export(ws, store, sink, ExportOptions())
    names = _members(bytes(sink.buffer))
    assert names[0] == "manifest.json"
    assert names[-1] == "TRAILER.json"


def test_every_member_digest_in_the_trailer_matches_the_bytes_that_arrived(ws, store):
    import hashlib

    sink = _Unseekable()
    stream_export(ws, store, sink, ExportOptions())
    raw = bytes(sink.buffer)
    trailer = ExportTrailer.model_validate_json(_member_bytes(raw, "TRAILER.json"))
    assert trailer.members, "a trailer that verifies nothing verifies nothing"
    for name, digest in trailer.members.items():
        payload = _member_bytes(raw, name)
        assert hashlib.sha256(payload).hexdigest() == digest.sha256, name
        assert len(payload) == digest.bytes, name


def test_the_archive_is_readable_by_tar_without_laurelin(ws, store, tmp_path):
    """An opaque archive would make the anti-lock-in claim self-refuting: the
    escape hatch has to work on a machine that has never heard of Laurelin."""
    archive = tmp_path / "export.tar"
    export_workspace(ws, store, archive, ExportOptions())

    listed = subprocess.run(
        ["tar", "tf", str(archive)], capture_output=True, text=True, check=True
    )
    assert "manifest.json" in listed.stdout.split("\n")

    extracted = subprocess.run(
        ["tar", "xOf", str(archive), "manifest.json"],
        capture_output=True, check=True,
    )
    assert json.loads(extracted.stdout)["format_version"] == 1


def test_the_archive_and_every_member_are_mode_0600_and_ownerless(ws, store, tmp_path):
    """An export FILE is a worse credential vector than an API response, and an
    archive that records who built it restores their ownership when extracted
    as root."""
    archive = tmp_path / "export.tar"
    export_workspace(ws, store, archive, ExportOptions())
    assert oct(archive.stat().st_mode & 0o777) == "0o600"

    with tarfile.open(archive, mode="r|*") as tar:
        seen = 0
        for member in tar:
            seen += 1
            assert member.mode == 0o600, member.name
            assert (member.uid, member.gid) == (0, 0), member.name
            assert (member.uname, member.gname) == ("", ""), member.name
        assert seen > 5


def test_export_refuses_to_overwrite_an_existing_archive(ws, store, tmp_path):
    archive = tmp_path / "export.tar"
    export_workspace(ws, store, archive, ExportOptions())
    with pytest.raises(ExportRefused, match="already exists"):
        export_workspace(ws, store, archive, ExportOptions())


def test_a_failed_export_never_leaves_a_file_with_the_real_name(ws, store, tmp_path):
    """A partial archive that looks complete is the failure this whole feature
    exists to prevent."""
    archive = tmp_path / "export.tar"
    (ws.pipelines_dir / "leaky.py").write_text("PASSWORD = 'hunter2'\n")
    with pytest.raises(ExportRefused):
        export_workspace(ws, store, archive, ExportOptions())
    assert not archive.exists()
    assert list(tmp_path.glob(".*partial")) == []


def test_an_append_versions_parts_travel_even_though_they_predate_it(ws, store):
    """An append's manifest references parts written for earlier versions, so
    the part set is a transitive union — a per-version walk would miss them and
    a directory listing would sweep every version's parts into one."""
    catalog = DatasetCatalog(ws, store)
    catalog.append("sales", pa.table({"region": ["fr"], "amount": [3]}))
    latest = store.get_version("sales")
    assert len(latest.files) == 2, "fixture no longer exercises an append"

    sink = _Unseekable()
    stream_export(ws, store, sink, ExportOptions())
    names = set(_members(bytes(sink.buffer)))
    for key in latest.files:
        assert key in names, key


def test_a_metadata_only_export_carries_no_parquet_and_says_so(ws, store):
    sink = _Unseekable()
    manifest = stream_export(ws, store, sink, ExportOptions(metadata_only=True))
    names = _members(bytes(sink.buffer))
    assert not [n for n in names if n.startswith("data/")]
    assert manifest.scope.data is False
    assert [d.data_state for d in manifest.datasets] == ["metadata_only"]


def test_export_refuses_when_a_pipeline_looks_like_it_holds_a_credential(ws, store):
    """pipelines/*.py is exec'd unsandboxed on every build, so the export will
    not quietly edit one — a transform changed behind the operator's back still
    runs, and computes something else."""
    (ws.pipelines_dir / "leaky.py").write_text(
        "CONN = 'postgresql://svc:hunter2@db.internal/prod'\n"
    )
    with pytest.raises(ExportRefused, match="credential"):
        preview_manifest(ws, store, ExportOptions())

    manifest = preview_manifest(ws, store, ExportOptions(allow_content_warnings=True))
    assert [(w.file, w.line) for w in manifest.content_warnings] == [
        ("pipelines/leaky.py", 1)
    ]


def test_export_refuses_in_multi_mode_without_a_membership_decision(ws, store):
    """In multi mode the effective role lives in control.db::workspace_members,
    outside the workspace — the single largest decision input there is."""
    with pytest.raises(ExportRefused, match="multi-workspace mode"):
        preview_manifest(ws, store, ExportOptions(mode="multi", multi_slug="acme"))

    declined = preview_manifest(
        ws, store,
        ExportOptions(mode="multi", multi_slug="acme", include_membership=False),
    )
    assert declined.scope.membership is False

    included = preview_manifest(
        ws, store,
        ExportOptions(
            mode="multi", multi_slug="acme", include_membership=True,
            membership_rows=({"slug": "acme", "username": "vic", "role": "editor"},),
        ),
    )
    assert included.tables["workspace_members"].rows == 1


def test_an_object_store_data_plane_refuses_rather_than_exporting_no_data(
    ws, store, monkeypatch
):
    """Streaming parts out of S3/GCS/Azure is unverified end-to-end, so a full
    export from one is opt-in. The alternative is an archive that looks fine
    and is missing its data."""
    from laurelin.core.storage import Storage

    class _Remote(Storage):
        is_remote = True

    remote = _Remote(*_storage_parts(ws))
    with pytest.raises(ExportRefused, match="object store"):
        preview_manifest(ws, store, ExportOptions(), storage=remote)

    # Metadata-only reads no parts at all, so it is unaffected.
    assert preview_manifest(
        ws, store, ExportOptions(metadata_only=True), storage=remote
    ).origin.data_plane == "remote"


def _storage_parts(ws):
    from laurelin.core.storage import storage_for

    real = storage_for(ws)
    return real.fs, real.base, "s3://bucket/prefix"


def test_the_export_streams_and_does_not_buffer_the_workspace(tmp_path):
    """Peak RSS must not track workspace size. 200 MB of parts through a 1 MiB
    buffer, asserted against the process high-water mark."""
    workspace = Workspace.init(tmp_path / "big", name="big")
    store = MetadataStore(workspace.metadata_path)
    catalog = DatasetCatalog(workspace, store)
    # Random bytes, not a repeated pattern: Parquet compressed 20 MB of b"x"
    # down to 4.5 KB, so the first version of this test measured nothing.
    def chunk():
        return pa.table({"blob": [os.urandom(1024) for _ in range(20_000)]})

    catalog.write("bulk", chunk())
    for _ in range(9):
        catalog.append("bulk", chunk())
    parts_dir = workspace.data_dir / "bulk" / "parts"
    total = sum(p.stat().st_size for p in parts_dir.iterdir())
    assert total > 150 * 1024 * 1024, f"fixture is only {total} bytes"

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    archive = tmp_path / "big.tar"
    export_workspace(workspace, store, archive, ExportOptions())
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert archive.stat().st_size > total
    # ru_maxrss is KiB on Linux and never decreases, so this is a high-water
    # delta: the export may not add a quarter gigabyte of resident memory
    # however large the workspace is.
    assert (after - before) < 256 * 1024, f"peak RSS grew by {(after - before)} KiB"


def test_dataset_restricts_the_data_but_never_the_governance(ws, store):
    """Governance is a closure: a partial one cannot be proven, so --dataset is
    offered for data only and the rules always travel whole."""
    catalog = DatasetCatalog(ws, store)
    catalog.write("hr", pa.table({"id": ["1"]}))
    store.set_explicit_markings("hr", [])

    sink = _Unseekable()
    manifest = stream_export(ws, store, sink, ExportOptions(datasets=("sales",)))
    states = {d.name: d.data_state for d in manifest.datasets}
    assert states == {"sales": "included", "hr": "metadata_only"}

    names = set(_members(bytes(sink.buffer)))
    assert not [n for n in names if n.startswith("data/hr/")]
    assert manifest.tables["datasets"].rows == 2, "governance was restricted too"


def test_the_spool_never_lands_in_the_system_temp_directory(ws, store, tmp_path):
    """The spool holds the whole governance configuration, and /tmp is
    world-traversable on a normal box."""
    import tempfile as _tempfile

    seen = []
    real = _tempfile.SpooledTemporaryFile

    def record(*args, **kwargs):
        seen.append(kwargs.get("dir"))
        return real(*args, **kwargs)

    _tempfile.SpooledTemporaryFile = record
    try:
        stream_export(ws, store, _Unseekable(), ExportOptions())
    finally:
        _tempfile.SpooledTemporaryFile = real
    assert seen and all(d == str(ws.root) for d in seen), seen
