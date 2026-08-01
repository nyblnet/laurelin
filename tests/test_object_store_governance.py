"""Row-level security and the materialization.

The guarantee, unchanged by the operational store and asserted here so it stays
unchanged: **a policied request never reads the materialization.** The store is
shared and built once for everybody; a per-user view has no business in it. So a
user with a policy on the backing dataset always falls through to the scan.

The second half is newer and is the reason the first half is not enough on its
own. The edit overlay used to be applied with no policy at all on every path: a
*created* object is a whole row that never passes through the policied scan, so
a create carrying ``realm='beleriand'`` was returned to a user restricted to
``realm='valinor'``. That was tolerable only because nothing durable held it.
Materializing the overlay into a shared table would have turned it into a
durable cross-tenant insert channel, which is why it is fixed here rather than
later.
"""

import pyarrow as pa
import pytest

from laurelin.catalog import DatasetCatalog
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User
from laurelin.core.permissions import PermissionService
from laurelin.ontology import OntologyService, load_ontology

ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      realm: {type: string}
      pop: {type: integer}
link_types:
  - api_name: same_realm
    from_type: city
    from_property: realm
    to_type: city
    to_property: realm
actions:
  - api_name: found
    object_type: city
    kind: create
    parameters:
      name: {type: string, required: true}
      realm: {type: string, required: true}
      pop: {type: integer}
  - api_name: rename
    object_type: city
    kind: update
    parameters:
      realm: {type: string, required: true}
"""


def cities() -> pa.Table:
    return pa.table({
        "name": [f"city-{i}" for i in range(6)],
        "realm": [["valinor", "beleriand"][i % 2] for i in range(6)],
        "pop": pa.array([100 + i for i in range(6)], type=pa.int64()),
    })


@pytest.fixture()
def env(tmp_path):
    ws = Workspace.init(tmp_path / "gov", name="gov")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", cities())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)
    ontology = load_ontology(ws.ontology_dir)

    store.set_dataset_policy("cities", {
        "dataset": "cities",
        "row_policy": {"column": "realm", "rules": [
            {"subject_kind": "user", "subject": "elf", "values": ["valinor"]},
        ]},
        "column_masks": [],
    })
    perms = PermissionService(store)
    elf = User(id="e", username="elf", role=Role.viewer)

    admin_svc = OntologyService(ws, catalog, store, ontology)
    elf_svc = OntologyService(
        ws, catalog, store, ontology,
        policy=perms.query_policy_fn(elf),
        policy_for=perms.per_dataset_policy_fn(elf),
        plan_for=perms.arrow_policy_fn(elf),
    )
    return admin_svc, elf_svc


def pks(result) -> set:
    return {o["__pk"] for o in result["objects"]}


# -- the guarantee that must not move ----------------------------------------

def test_rls_users_never_read_the_materialization(env):
    """Renamed from test_rls_users_never_read_the_index, same property: the
    store is shared, so a per-user view must never be served from it."""
    admin, elf = env
    admin.reindex("city")
    ot = elf.ontology.object_type("city")
    assert admin.store_is_caught_up(ot)
    assert elf._index_query(ot, None, None, 10, 0) is None
    assert pks(elf.query("city", limit=10)) == {"city-0", "city-2", "city-4"}


def test_a_policied_key_lookup_does_not_come_from_the_store(env):
    """get() now goes through the store first, which would have been a hole:
    a point lookup is the cheapest possible way to ask "does this row exist"."""
    admin, elf = env
    admin.reindex("city")
    assert elf.get("city", "city-1") is None, "hidden row stays hidden"
    assert elf.get("city", "city-0") is not None


def test_link_traversal_stays_policied(env):
    admin, elf = env
    admin.reindex("city")
    linked = elf.linked("city", "city-0", "same_realm")
    assert {o["__pk"] for o in linked} == {"city-0", "city-2", "city-4"}


def test_aggregation_stays_policied(env):
    admin, elf = env
    admin.reindex("city")
    groups = elf.aggregate("city", group_by=["realm"])["groups"]
    assert {g["realm"] for g in groups} == {"valinor"}
    assert sum(g["count"] for g in groups) == 3


# -- the overlay ---------------------------------------------------------------

def test_a_create_outside_the_policy_is_not_visible_to_a_policied_user(env):
    """The durable cross-tenant insert channel, closed."""
    admin, elf = env
    admin.apply_action("found", pk=None,
                       parameters={"name": "smuggled", "realm": "beleriand", "pop": 1})

    assert "smuggled" in pks(admin.query("city", limit=20))
    assert "smuggled" not in pks(elf.query("city", limit=20))
    assert elf.get("city", "smuggled") is None


def test_the_create_is_hidden_on_every_path(env):
    """Four paths answer object queries and a policy that means four things is
    not a policy."""
    admin, elf = env
    admin.apply_action("found", pk=None,
                       parameters={"name": "smuggled", "realm": "beleriand"})
    ot = elf.ontology.object_type("city")

    assert "smuggled" not in {o["__pk"] for o in elf._materialize(ot)}
    scanned = elf._sql_query(ot, None, None, 50, 0)
    if scanned is not None:
        assert "smuggled" not in {o["__pk"] for o in scanned["objects"]}
    assert "smuggled" not in pks(elf.query("city", limit=50))
    assert {o["__pk"] for o in elf.linked("city", "city-0", "same_realm")} == {
        "city-0", "city-2", "city-4"}
    groups = elf.aggregate("city", group_by=["realm"])["groups"]
    assert {g["realm"] for g in groups} == {"valinor"}


def test_a_create_inside_the_policy_is_visible(env):
    """Fail closed, not fail useless: the check must still admit legitimate
    creates, or it is just a way of hiding data from its own author."""
    admin, elf = env
    admin.apply_action("found", pk=None,
                       parameters={"name": "tirion", "realm": "valinor", "pop": 2})
    assert "tirion" in pks(elf.query("city", limit=20))
    assert elf.get("city", "tirion") is not None


def test_a_create_that_omits_the_policy_column_is_excluded(env):
    """A null is never "in" an allowlist, and a row we cannot place in a
    partition is a row we cannot show."""
    admin, elf = env
    ot = admin.ontology.object_type("city")
    import uuid

    from laurelin.core.models import EditKind, ObjectEdit
    admin.store.add_object_edit(ObjectEdit(
        id=uuid.uuid4().hex, object_type="city", pk_value="nowhere",
        kind=EditKind.create, payload={"name": "nowhere", "pop": 3}, actor="t",
    ))
    assert "nowhere" in {o["__pk"] for o in admin._materialize(ot)}
    assert "nowhere" not in pks(elf.query("city", limit=20))


# -- who builds the materialization -------------------------------------------

def test_a_policied_editor_does_not_bake_their_view_into_the_shared_store(env):
    """reindex materializes under a system identity. The rebuild endpoint is
    only EDITOR-gated, so without this an ordinary editor with a row policy
    narrows the index for everybody."""
    admin, elf = env
    assert elf.reindex("city") == 6, "built unpoliced, whoever asked"
    assert admin.query("city", limit=20)["total"] == 6
    assert admin.store_is_caught_up(admin.ontology.object_type("city"))
    # …and the policied user still sees only their own rows, from the scan.
    assert pks(elf.query("city", limit=20)) == {"city-0", "city-2", "city-4"}


def test_a_policied_write_does_not_write_a_masked_row_into_the_store(env):
    """The pre-image for a write comes from the store itself, never from the
    caller's policied view, so nothing user-shaped can reach a shared row."""
    admin, elf = env
    admin.reindex("city")
    elf.apply_action("rename", pk="city-0", parameters={"realm": "valinor"})

    row = admin.store.object_index_rows("city", ["city-0"])[0]
    import json
    props = json.loads(row["props_json"])
    assert props["pop"] == 100, "the untouched property survived the write"
    assert props["name"] == "city-0"


def test_the_type_detail_does_not_disclose_the_unpoliced_object_count(env):
    """``index.objects`` is read straight off the shared state row, so it
    counts *every* tenant's objects. A user whose row-level security shows them
    three of six was told the type had six — cross-tenant cardinality through
    the materialization — and ``applied_seq`` sits beside it as a global
    write-volume signal. They are operator numbers, so they go to callers the
    dataset policy does not narrow."""
    from laurelin.api.routes import get_object_type
    from laurelin.core.models import Role, User

    admin, elf = env
    admin.reindex("city")
    perms = PermissionService(admin.store)
    root = User(id="r", username="root", role=Role.admin)
    elf_user = User(id="e", username="elf", role=Role.viewer)

    unpoliced = get_object_type("city", admin, perms, root)["index"]
    assert unpoliced["objects"] == 6 and unpoliced["lag"] == 0

    policied = get_object_type("city", elf, perms, elf_user)["index"]
    assert policied["objects"] is None, "the shared count is not this user's count"
    assert policied["applied_seq"] is None and policied["lag"] is None
    # "Is it materialized at all" stays visible: it is a property of the type,
    # not of anyone's rows.
    assert policied["indexed"] is True


def test_a_policied_create_cannot_overwrite_a_hidden_object(env):
    """The other half of the cross-tenant insert channel: the write half.

    ``_policy_admits`` closed the read half — which overlay creates a policied
    user gets *back*. Nothing filtered what one *wrote*. A create for a key
    that already exists is a replacement: it inherits the hidden row's ordinal
    in the shared store, and a writeback folds it over that row in the dataset.
    Measured before the fix: an editor confined to ``valinor`` replaced a
    ``beleriand`` row, the store, the victim's scan and the attacker's scan
    each answered differently for one key, and the fold deleted the victim's
    data for everyone. EDITOR on the object type was the only grant needed.
    """
    admin, elf = env
    admin.reindex("city")
    assert elf.get("city", "city-1") is None, "the target is hidden from the attacker"

    with pytest.raises(ValueError, match="already exists"):
        elf.apply_action("found", pk=None, parameters={
            "name": "city-1", "realm": "valinor", "pop": 1})

    assert admin.store.max_edit_seq("city") == 0, "nothing was recorded"
    victim = admin.get("city", "city-1")
    assert victim["realm"] == "beleriand" and victim["pop"] == 101

    # A key that exists nowhere is still creatable, and it is still subject to
    # the read-side policy check.
    elf.apply_action("found", pk=None, parameters={
        "name": "brand-new", "realm": "valinor", "pop": 7})
    assert elf.get("city", "brand-new")["realm"] == "valinor"


def test_an_unpoliced_create_still_replaces_in_place(env):
    """The refusal above is scoped to callers a dataset policy narrows. For an
    admin, create-over-existing is the documented replacement behaviour and is
    not a cross-tenant operation — narrowing it for them would be a different
    feature removal wearing a security fix's clothes."""
    admin, _elf = env
    admin.reindex("city")
    admin.apply_action("found", pk=None, parameters={
        "name": "city-1", "realm": "replaced", "pop": 9})
    assert admin.get("city", "city-1")["realm"] == "replaced"
    assert admin.query("city", limit=20)["total"] == 6, "replaced, not added"


def test_the_existence_check_does_not_leak_hidden_keys(env):
    """Hidden-but-existing and genuinely-absent must be the same answer, or the
    write path becomes an enumeration oracle over other tenants' keys."""
    admin, elf = env
    admin.reindex("city")
    hidden, absent = None, None
    try:
        elf.apply_action("rename", pk="city-1", parameters={"realm": "x"})
    except ValueError as exc:
        hidden = str(exc)
    try:
        elf.apply_action("rename", pk="does-not-exist", parameters={"realm": "x"})
    except ValueError as exc:
        absent = str(exc)
    assert hidden is not None and absent is not None
    assert hidden.replace("city-1", "K") == absent.replace("does-not-exist", "K")
    assert admin.store.max_edit_seq("city") == 0, "no edit was recorded either way"
