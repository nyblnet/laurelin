"""The moat test: a reconstructed workspace must govern identically.

"The tables copied" is not the claim. The claim is that after a round trip the
SAME principal gets the SAME rows and the SAME masked cells — checked through
all three enforcement paths, because a round trip can preserve one renderer and
break another.

The fixture is built through the real APIs rather than by writing rows. In
particular ``derived`` is produced by *running a transform*, so ``lineage_edges``
exists and ``pii`` reaches a dataset nobody marked; direct-writing it would
leave the single most non-obvious member of the governance closure untested
while this file still looked thorough.

``acl_only`` exists because of a measurement: on ``sales``, deleting every
dataset grant changed nothing, because ``_has_clearance`` still denied. Without
a dataset whose grants are the *only* gate, the containment assertion passes
vacuously.
"""

import os
import uuid
from decimal import Decimal

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import (
    ColumnMask,
    DatasetPolicy,
    Grant,
    MaskMode,
    PolicySubject,
    Role,
    RowPolicy,
    RowRule,
    SubjectKind,
    User,
)
from laurelin.core.permissions import PermissionService
from laurelin.export import (
    ExportOptions,
    ImportOptions,
    contains,
    diff_fingerprints,
    export_workspace,
    governance_fingerprint,
    import_workspace,
    widenings,
)
from laurelin.transforms import Builder, collect_transforms

PG_URL = os.environ.get("LAURELIN_TEST_POSTGRES")

PIPELINE = (
    "from laurelin.transforms import transform, Input, Output\n"
    "@transform(output=Output('derived'), s=Input('sales'))\n"
    "def derive(s):\n"
    "    return s\n"
)

ONTOLOGY = """
object_types:
  - api_name: sale
    backing_dataset: sales
    primary_key: region
    properties:
      region: {type: string}
      ssn: {type: string}
"""

VIC = User(id="u-vic", username="vic", role=Role.viewer)     # cleared analyst
MAL = User(id="u-mal", username="mal", role=Role.viewer)     # negative control
ADM = User(id="u-adm", username="adm", role=Role.admin)
PRINCIPALS = [VIC, MAL, ADM, None]
OBJECT_TYPES = {"sale": "sales"}


@pytest.fixture(params=["sqlite", "postgres"])
def dialect(request):
    if request.param == "postgres" and not PG_URL:
        pytest.skip("set LAURELIN_TEST_POSTGRES to a postgresql:// URL to run")
    return request.param


@pytest.fixture()
def make_store(dialect):
    schemas: list[str] = []

    def factory(workspace: Workspace) -> MetadataStore:
        if dialect == "sqlite":
            return MetadataStore(workspace.metadata_path)
        schema = "gv_" + uuid.uuid4().hex[:12]
        schemas.append(schema)
        return MetadataStore(PG_URL, schema=schema)

    yield factory

    if schemas:
        import psycopg

        with psycopg.connect(PG_URL) as conn:
            for schema in schemas:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()


def _sales_rows(regions, ssns, amounts) -> pa.Table:
    # decimal(12,2) because tests/test_clickhouse_governance.py already proved
    # text-rendering divergence on exactly this type: a policy defined on the
    # *text* of a value means different things in different engines.
    return pa.table({
        "region": pa.array(regions, pa.string()),
        "ssn": pa.array(ssns, pa.string()),
        "amount": pa.array(
            [Decimal(a) for a in amounts], pa.decimal128(12, 2)
        ),
    })


def _write_sales_history(catalog) -> None:
    """Three versions of the governed dataset, each a full rewrite.

    Rewrites rather than appends, and not by preference: ``catalog.append``
    cannot append to a dataset holding a decimal column at all. Measured on a
    clean workspace with no export code involved::

        catalog.write("d", <decimal128(12,2)>)   # fine
        catalog.append("d", <same table>)
        ValueError: No type alias for decimal128(12, 2)

    ``append`` rebuilds the target schema with ``pa.type_for_alias(c.type)``
    over the stored type *strings* (catalog.py:374), and there is no alias to
    parse ``'decimal128(12, 2)'`` back. ``timestamp[us]`` survives the same
    round trip, which is why this has stayed hidden. Filed separately; the
    decimal column stays because a money column is exactly where text-rendering
    divergence between engines shows up, and dropping it to dodge the bug would
    quietly reduce what this proof covers.

    The append case — a manifest referencing parts written for *earlier*
    versions — is carried by ``acl_only`` instead.
    """
    catalog.write("sales", _sales_rows(["eu", "eu", "us"],
                                       ["111", "222", "333"],
                                       ["1.10", "2.20", "3.30"]))
    catalog.write("sales", _sales_rows(["eu", "eu", "us", "eu", "ap"],
                                       ["111", "222", "333", "444", "555"],
                                       ["1.10", "2.20", "3.30", "4.40", "5.50"]))
    catalog.write("sales", _sales_rows(["eu", "eu", "us", "eu", "ap", "us"],
                                       ["111", "222", "333", "444", "555", "666"],
                                       ["1.10", "2.20", "3.30", "4.40", "5.50",
                                        "6.60"]))


@pytest.fixture()
def governed(tmp_path, make_store):
    workspace = Workspace.init(tmp_path / "src", name="Acme Production")
    store = make_store(workspace)
    catalog = DatasetCatalog(workspace, store)
    _write_sales_history(catalog)
    # Two appends, so acl_only's latest version is a manifest spanning three
    # parts, two of which predate it (catalog.py:344). That is the case a naive
    # exporter gets wrong — walking one version's directory, or globbing
    # non-recursively, produces an archive that restores a dataset silently
    # missing its older rows: fewer rows, no error, which is indistinguishable
    # from a working row policy.
    catalog.write("acl_only", pa.table({"id": ["a", "b"], "note": ["x", "y"]}))
    catalog.append("acl_only", pa.table({"id": ["c"], "note": ["z"]}))
    catalog.append("acl_only", pa.table({"id": ["d"], "note": ["w"]}))
    (workspace.pipelines_dir / "p.py").write_text(PIPELINE)
    (workspace.ontology_dir / "o.yml").write_text(ONTOLOGY)

    # A real build, so lineage_edges is real and pii reaches `derived` without
    # anyone marking it.
    Builder(workspace, catalog, store,
            collect_transforms(workspace.pipelines_dir)).build()
    assert store.get_dataset("derived") is not None

    from laurelin.core.auth import AuthService

    auth = AuthService(store)
    auth.create_user("vic", "pw-vic-longenough", Role.viewer, actor="setup")
    auth.create_user("mal", "pw-mal-longenough", Role.viewer, actor="setup")
    auth.create_user("adm", "pw-adm-longenough", Role.admin, actor="setup")

    store.create_group("analysts", "2026-01-01T00:00:00Z")
    store.create_group("auditors", "2026-01-01T00:00:00Z")
    store.set_group_members("analysts", ["vic"])
    store.set_group_members("auditors", ["mal"])

    store.create_marking("pii", "personally identifying")
    store.set_explicit_markings("sales", ["pii"])
    store.recompute_all_markings()
    store.set_clearances("vic", ["pii"])
    store.set_clearances("mal", [])

    view_by_analysts = [
        Grant(subject_kind=SubjectKind.group, subject="analysts",
              can_view=True).model_dump()
    ]
    store.set_grants_for_dataset("sales", view_by_analysts)
    store.set_grants_for_dataset("acl_only", view_by_analysts)
    # can_edit, not can_view: an ontology grant that only grants view is
    # indistinguishable from the no-grants default (any authenticated user may
    # view a type), so deleting it would change no decision and the fixture
    # would silently stop testing ontology_grants at all. Elevating a viewer to
    # edit one type is the documented point of these grants.
    store.set_grants_for_type("sale", [
        Grant(subject_kind=SubjectKind.group, subject="analysts",
              can_edit=True).model_dump()
    ])

    store.set_dataset_policy("sales", DatasetPolicy(
        dataset="sales",
        row_policy=RowPolicy(column="region", rules=[
            RowRule(subject_kind=SubjectKind.group, subject="analysts",
                    values=["eu"]),
        ]),
        # Order is load-bearing: redact then hash would digest '***' on the
        # table path and the plaintext on a renderer that collapsed the masks.
        column_masks=[
            ColumnMask(column="ssn", mode=MaskMode.redact),
            ColumnMask(column="amount", mode=MaskMode.null, exempt=[
                PolicySubject(subject_kind=SubjectKind.group, subject="auditors"),
            ]),
        ],
    ).model_dump(mode="json"))
    return workspace, store


@pytest.fixture()
def target(tmp_path, make_store):
    workspace = Workspace.init(tmp_path / "dst", name="dst")
    return workspace, make_store(workspace)


def _fingerprint(workspace, store):
    return governance_fingerprint(
        store, DatasetCatalog(workspace, store), PRINCIPALS,
        datasets=["sales", "derived", "acl_only"], object_types=OBJECT_TYPES,
    )


def _roundtrip(governed, target, tmp_path):
    src_ws, src_store = governed
    dst_ws, dst_store = target
    archive = tmp_path / "export.tar"
    export_workspace(src_ws, src_store, archive, ExportOptions(created_by="andy"))
    report = import_workspace(archive, dst_ws, dst_store, ImportOptions())
    assert report.applied
    return report


def _rebind(target, analysts=("vic",), auditors=("mal",)):
    """The explicit, audited admin act the import deliberately does not do.

    These are the calls the admin routes make — POST /users, PUT
    /groups/{name}/members, PUT /users/{u}/clearances — reached through the
    service layer so the same test runs against a Postgres schema, which
    ``create_app`` has no argument for.
    """
    dst_ws, dst_store = target
    from laurelin.core.auth import AuthService

    auth = AuthService(dst_store)
    for name, role in (("vic", Role.viewer), ("mal", Role.viewer), ("adm", Role.admin)):
        if dst_store.get_user(name) is None:
            auth.create_user(name, f"pw-{name}-longenough", role, actor="ada")
    dst_store.set_group_members("analysts", list(analysts))
    dst_store.set_group_members("auditors", list(auditors))
    dst_store.set_clearances("vic", ["pii"])
    dst_store.set_clearances("mal", [])


# --------------------------------------------------------------------------- tests

def test_the_fixture_actually_governs_something(governed):
    """A containment test over a workspace that denies nobody proves nothing."""
    src_ws, src_store = governed
    perms = PermissionService(src_store)
    assert perms.dataset_permission(VIC, "sales") == (True, False)
    assert perms.dataset_permission(MAL, "sales") == (False, False)
    assert perms.dataset_permission(MAL, "acl_only") == (False, False)
    assert src_store.get_effective_markings("derived") == ["pii"], (
        "the transform did not propagate pii, so lineage is not being exercised"
    )
    assert len(src_store.list_versions("sales")) == 3, (
        "the version-history fixture collapsed to a single write"
    )
    appended = src_store.get_version("acl_only")
    assert len(appended.files) == 3, (
        "acl_only's latest version should be a manifest spanning three parts, "
        "two of which predate it; without that nothing here exercises an append"
    )

    fingerprint = _fingerprint(src_ws, src_store)
    vic_sales = fingerprint["cells"]["vic|sales"]
    adm_sales = fingerprint["cells"]["adm|sales"]
    assert vic_sales["table"] != adm_sales["table"], "the policy masks nothing"
    assert vic_sales["table"] == vic_sales["arrow"] == vic_sales["duckdb"], (
        "the three enforcement paths already disagree at the source"
    )

    # The masking must be visible in the *cell* sets, not merely in a digest,
    # or the containment check below is comparing something that cannot move.
    assert len(adm_sales["rows"]["table"]) == 6, "all three versions should be readable"
    assert len(vic_sales["rows"]["table"]) == 3, (
        "the row policy should hold vic to the eu rows across all three versions"
    )
    plaintext_ssn = set(adm_sales["visible"]["table"]) - set(vic_sales["visible"]["table"])
    assert plaintext_ssn, "the ssn redact mask hides nothing from vic"


def test_an_unbound_import_can_only_narrow_never_widen(governed, target, tmp_path):
    """Run BEFORE rebind. Equality after rebind would hide the failure, because
    rebind repairs the widening before the assertion executes."""
    src_ws, src_store = governed
    source = _fingerprint(src_ws, src_store)
    _roundtrip(governed, target, tmp_path)
    dst_ws, dst_store = target
    unbound = _fingerprint(dst_ws, dst_store)

    # widenings() rather than contains(): a bare boolean makes pytest print two
    # dicts of hashes, and the one thing worth knowing — *what* got wider — is
    # the part that would be missing.
    wider = {
        key: notes
        for key, target_cell in unbound["cells"].items()
        if (notes := widenings(source["cells"].get(key), target_cell))
    }
    assert not wider, f"the import widened access: {wider}"
    for key, pair in unbound["object_types"].items():
        source_pair = source["object_types"].get(key, [False, False])
        assert pair[0] <= source_pair[0] and pair[1] <= source_pair[1], key

    # And it really did narrow for somebody, or the assertion above is vacuous.
    assert source["cells"]["vic|sales"]["can_view"] is True
    assert unbound["cells"]["vic|sales"]["can_view"] is False


def test_the_same_principal_gets_the_same_rows_and_the_same_masked_cells_after_a_round_trip(
    governed, target, tmp_path
):
    """The equality assertion, after rebinding only what manifest.withheld and
    manifest.principals said had to be re-supplied."""
    src_ws, src_store = governed
    source = _fingerprint(src_ws, src_store)
    _roundtrip(governed, target, tmp_path)
    _rebind(target)
    dst_ws, dst_store = target
    rebound = _fingerprint(dst_ws, dst_store)

    differences = diff_fingerprints(source, rebound)
    assert not differences, (
        "the reconstructed workspace answers differently:\n"
        + "\n".join(
            f"  {d['cell' if 'cell' in d else 'object_type']}: "
            f"{d.get('changes', d)}"
            for d in differences
        )
    )


def test_a_principal_who_saw_nothing_at_the_source_sees_nothing_at_every_stage(
    governed, target, tmp_path
):
    """`mal` is the negative control, checked at import, after a faithful
    rebind, and against the hostile rebind that is the only way to widen."""
    src_ws, src_store = governed
    dst_ws, dst_store = target
    assert PermissionService(src_store).dataset_permission(MAL, "acl_only") == (
        False, False
    )

    _roundtrip(governed, target, tmp_path)
    perms = PermissionService(dst_store)
    assert perms.dataset_permission(MAL, "acl_only") == (False, False)
    assert perms.dataset_permission(MAL, "sales") == (False, False)

    _rebind(target)
    assert perms.dataset_permission(MAL, "acl_only") == (False, False)

    # Measured: putting an outsider into the destination's `analysts` flips
    # acl_only from (False, False) to (True, False). Import must never be the
    # thing that does this — an admin has to, explicitly and audibly.
    dst_store.set_group_members("analysts", ["mal"])
    assert perms.dataset_permission(MAL, "acl_only") == (True, False)


def test_a_grant_list_survives_so_the_dataset_does_not_fail_open(
    governed, target, tmp_path
):
    """permissions.py:333: no grants means readable by every authenticated
    viewer. Measured on acl_only, whose only gate is its grants — on sales the
    same deletion changed nothing, because clearance still denied."""
    _roundtrip(governed, target, tmp_path)
    dst_ws, dst_store = target
    assert len(dst_store.grants_for_dataset("acl_only")) == 1
    perms = PermissionService(dst_store)
    assert perms.dataset_permission(MAL, "acl_only") == (False, False)

    with dst_store._conn() as c:
        c.execute("DELETE FROM dataset_grants WHERE dataset = 'acl_only'")
    assert perms.dataset_permission(MAL, "acl_only") == (True, False), (
        "if this no longer widens, the fixture stopped proving why grants "
        "must import even when their subject does not resolve"
    )


def test_dropping_lineage_declassifies_the_downstream_dataset(
    governed, target, tmp_path
):
    """The direct regression that pins lineage_edges as PORTABLE."""
    _roundtrip(governed, target, tmp_path)
    dst_ws, dst_store = target
    assert dst_store.get_effective_markings("derived") == ["pii"]
    perms = PermissionService(dst_store)
    assert perms.dataset_permission(MAL, "derived") == (False, False)

    with dst_store._conn() as c:
        c.execute("DELETE FROM lineage_edges")
    dst_store.recompute_all_markings()
    assert dst_store.get_effective_markings("derived") == []
    assert perms.dataset_permission(MAL, "derived") == (True, False)


def test_clearances_travel_in_the_archive_but_are_never_applied_by_import(
    governed, target, tmp_path
):
    """A clearance is the one row type whose only possible effect is to widen:
    _has_clearance is `needed <= set(get_clearances(user))`."""
    report = _roundtrip(governed, target, tmp_path)
    _, dst_store = target
    assert report.rows_quarantined["clearances"] == 1
    assert dst_store.get_clearances("vic") == []
    assert PermissionService(dst_store).dataset_permission(VIC, "sales") == (
        False, False
    )


def test_the_policy_json_survives_byte_for_byte(governed, target, tmp_path):
    """Re-serializing would reorder keys, and byte identity is what makes the
    round trip a proof rather than a semantic argument."""
    _, src_store = governed
    _roundtrip(governed, target, tmp_path)
    _, dst_store = target
    with src_store._conn() as c:
        before = c.execute(
            "SELECT policy_json FROM dataset_policies WHERE dataset = 'sales'"
        ).fetchone()["policy_json"]
    with dst_store._conn() as c:
        after = c.execute(
            "SELECT policy_json FROM dataset_policies WHERE dataset = 'sales'"
        ).fetchone()["policy_json"]
    assert before == after


def test_the_manifest_lists_every_principal_the_rules_name(governed, tmp_path):
    from laurelin.export import preview_manifest

    src_ws, src_store = governed
    manifest = preview_manifest(src_ws, src_store, ExportOptions())
    named = {(p.kind, p.name) for p in manifest.principals}
    assert ("group", "analysts") in named
    assert ("group", "auditors") in named, (
        "a mask exemption names a group, and the rebind checklist has to say so"
    )
    assert ("user", "vic") in named


# ------------------------------------------------------- negative controls
#
# A proof that cannot fail is not a proof. Each of these reconstructs the
# workspace faithfully, confirms the equality assertion passes, then perturbs
# exactly one thing and requires the assertion to fire. The pair of directions
# matters as much as the failures: `widenings` must stay silent when the
# destination shows *less*, or it is an always-true predicate that would pass
# the containment test no matter what the import did.


def _rebound_pair(governed, target, tmp_path):
    """A faithful round trip, asserted clean before anything is perturbed."""
    src_ws, src_store = governed
    source = _fingerprint(src_ws, src_store)
    _roundtrip(governed, target, tmp_path)
    _rebind(target)
    dst_ws, dst_store = target
    assert diff_fingerprints(source, _fingerprint(dst_ws, dst_store)) == [], (
        "the round trip is not clean, so nothing below is a controlled test"
    )
    return source


def test_the_proof_catches_a_destination_that_returns_one_extra_row(
    governed, target, tmp_path
):
    """The headline failure: one row more than the source, and nothing else.

    One row, not a rewritten dataset, because the interesting bug is the small
    one — a row policy that lost a rule still returns *mostly* the right
    answer, and an assertion that only notices wholesale divergence would pass.
    """
    source = _rebound_pair(governed, target, tmp_path)
    dst_ws, dst_store = target

    # The source's six rows plus one, region 'eu' so it lands *inside* vic's
    # row policy rather than outside it — a seventh row she must not see would
    # be filtered and prove nothing.
    DatasetCatalog(dst_ws, dst_store).write(
        "sales",
        _sales_rows(["eu", "eu", "us", "eu", "ap", "us", "eu"],
                    ["111", "222", "333", "444", "555", "666", "777"],
                    ["1.10", "2.20", "3.30", "4.40", "5.50", "6.60", "7.70"]),
    )
    perturbed = _fingerprint(dst_ws, dst_store)

    assert diff_fingerprints(source, perturbed), "one extra row went unnoticed"
    gained = widenings(source["cells"]["vic|sales"], perturbed["cells"]["vic|sales"])
    assert gained, "the containment check did not see the extra row as widening"
    assert any("rows.table" in note for note in gained), gained
    assert any("rows.duckdb" in note for note in gained), (
        "the extra row was caught on one enforcement path but not the SQL one"
    )
    assert len(perturbed["cells"]["vic|sales"]["rows"]["table"]) == 4


def test_the_proof_catches_a_column_mask_that_stopped_being_applied(
    governed, target, tmp_path
):
    """The same rows, one of them no longer redacted.

    Row containment alone cannot see this — the row count is identical. It is
    the visible (column, value) set that moves, because the source's pair is
    ('ssn', '***') and the destination's is the plaintext.
    """
    source = _rebound_pair(governed, target, tmp_path)
    dst_ws, dst_store = target

    dst_store.set_dataset_policy("sales", DatasetPolicy(
        dataset="sales",
        row_policy=RowPolicy(column="region", rules=[
            RowRule(subject_kind=SubjectKind.group, subject="analysts",
                    values=["eu"]),
        ]),
        column_masks=[  # the ssn redact mask is gone; everything else is intact
            ColumnMask(column="amount", mode=MaskMode.null, exempt=[
                PolicySubject(subject_kind=SubjectKind.group, subject="auditors"),
            ]),
        ],
    ).model_dump(mode="json"))
    perturbed = _fingerprint(dst_ws, dst_store)

    assert diff_fingerprints(source, perturbed), "a dropped mask went unnoticed"
    gained = widenings(source["cells"]["vic|sales"], perturbed["cells"]["vic|sales"])
    assert any("visible.table" in note for note in gained), gained
    assert len(perturbed["cells"]["vic|sales"]["rows"]["table"]) == len(
        source["cells"]["vic|sales"]["rows"]["table"]
    ), "this control is only meaningful if the row count did not move"


def test_the_proof_catches_a_group_that_gained_an_outsider(
    governed, target, tmp_path
):
    """The hostile rebind, expressed as a fingerprint rather than a boolean.

    Measured on this fixture: putting `mal` into the destination's `analysts`
    flips acl_only from (False, False) to (True, False). Import must never do
    this, and if it ever does, the containment check has to be what says so.
    """
    source = _rebound_pair(governed, target, tmp_path)
    dst_ws, dst_store = target

    dst_store.set_group_members("analysts", ["vic", "mal"])
    perturbed = _fingerprint(dst_ws, dst_store)

    gained = widenings(
        source["cells"]["mal|acl_only"], perturbed["cells"]["mal|acl_only"]
    )
    assert any("can_view" in note for note in gained), gained
    assert widenings(
        source["cells"]["mal|sales"], perturbed["cells"]["mal|sales"]
    ), "mal joined analysts and gained sales rows without the check firing"


def test_narrowing_is_not_reported_as_widening(governed, target, tmp_path):
    """The other direction, and the reason the two assertions are separate.

    Dropping a membership makes the destination answer with *less*. The
    equality assertion must fire — the round trip is no longer faithful — and
    the containment assertion must stay silent, because an unbound import is
    expected to narrow. Without this test, `widenings` returning [] for every
    input would still pass every other test in this file.
    """
    source = _rebound_pair(governed, target, tmp_path)
    dst_ws, dst_store = target

    dst_store.set_group_members("analysts", [])
    perturbed = _fingerprint(dst_ws, dst_store)

    assert diff_fingerprints(source, perturbed), (
        "vic lost her group and the equality assertion did not notice"
    )
    assert source["cells"]["vic|sales"]["can_view"] is True
    assert perturbed["cells"]["vic|sales"]["can_view"] is False
    for key, cell in perturbed["cells"].items():
        assert widenings(source["cells"].get(key), cell) == [], (
            f"{key}: narrowing was reported as widening, so the containment "
            "check is just an equality check wearing a different name"
        )
        # contains() is the boolean the CLI and the verification panel call;
        # it must agree with the list form it wraps.
        assert contains(source["cells"].get(key), cell), key


def test_every_declared_governance_table_actually_changes_a_decision(
    governed, tmp_path, make_store
):
    """Keeps the classification honest rather than aspirational.

    Delete each table named in ``GOVERNANCE_TABLES`` from a *freshly*
    reconstructed destination and at least one fingerprint cell must move.
    This is the guard that would have caught ``groups`` and ``markings`` being
    called enforcement inputs: measured, deleting either of those (keeping
    ``group_members`` and ``dataset_markings``) changes nothing at all, which
    is why they are portable-for-authoring and not on this list.

    One destination per table, rather than accumulating deletions into one, is
    not tidiness. The first version of this test shared a destination and
    ``ontology_grants`` passed vacuously: ``dataset_grants`` had already been
    deleted a step earlier, which is exactly the kind of conditional pass this
    guard exists to catch in the classification.
    """
    from laurelin.export.manifest import GOVERNANCE_TABLES

    src_ws, src_store = governed
    archive = tmp_path / "guard.tar"
    export_workspace(src_ws, src_store, archive, ExportOptions())

    for index, table in enumerate(GOVERNANCE_TABLES):
        workspace = Workspace.init(tmp_path / f"g{index}", name=f"g{index}")
        store = make_store(workspace)
        import_workspace(archive, workspace, store, ImportOptions())
        _rebind((workspace, store))

        before = _fingerprint(workspace, store)
        with store._conn() as c:
            rows = c.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        assert rows, f"{table} is empty at the destination, so deleting it proves nothing"
        with store._conn() as c:
            c.execute(f"DELETE FROM {table}")
        store.recompute_all_markings()
        after = _fingerprint(workspace, store)
        assert diff_fingerprints(before, after), (
            f"deleting {table} changed no decision, so it is not a governance "
            "input and should not be classified as one"
        )
