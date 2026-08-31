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
from laurelin.core.approvals import ChangeTicket as _ChangeTicket
from laurelin.core.config import Workspace
from laurelin.core.db import MetadataStore
from laurelin.core.models import Role, User
from laurelin.core.permissions import PermissionService
from laurelin.ontology import OntologyService, load_ontology

_TICKET = _ChangeTicket(kind="local", actor="test")

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
    }, ticket=_TICKET)
    perms = PermissionService(store)
    elf = User(id="e", username="elf", role=Role.viewer)

    admin_svc = OntologyService(ws, catalog, store, ontology)
    elf_svc = OntologyService(
        ws, catalog, store, ontology,
        policy=perms.query_policy_fn(elf),
        policy_for=perms.per_dataset_policy_fn(elf),
        plan_for=perms.arrow_policy_fn(elf),
        decide_for=lambda ds, cols, _u=elf: perms.decide(ds, cols, _u),
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


# -- writing through the policy ------------------------------------------------
#
# `_policy_admits` and `_refuse_shadowing_create` are both about *creates*. An
# update looked safe by comparison: it cannot introduce a row, it merges onto a
# base row that already survived the policy. Two things still got through, and
# both are here.

MASKED_ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      realm: {type: string}
      pop: {type: integer}
      founder: {type: string}
actions:
  - api_name: rename
    object_type: city
    kind: update
    parameters:
      realm: {type: string, required: true}
  - api_name: recount
    object_type: city
    kind: update
    parameters:
      pop: {type: integer, required: true}
  - api_name: appoint
    object_type: city
    kind: update
    parameters:
      founder: {type: string, required: true}
  - api_name: refound
    object_type: city
    kind: update
    parameters:
      founder: {type: string, required: true}
      pop: {type: integer, required: true}
  - api_name: rekey
    object_type: city
    kind: update
    parameters:
      name: {type: string, required: true}
  - api_name: raze
    object_type: city
    kind: delete
    parameters: {}
  - api_name: found
    object_type: city
    kind: create
    parameters:
      name: {type: string, required: true}
      realm: {type: string, required: true}
      pop: {type: integer}
      founder: {type: string}
"""


def founded_cities() -> pa.Table:
    return pa.table({
        "name": [f"city-{i}" for i in range(6)],
        "realm": [["valinor", "beleriand"][i % 2] for i in range(6)],
        "pop": pa.array([100 + i for i in range(6)], type=pa.int64()),
        "founder": [f"founder-{i}" for i in range(6)],
    })


@pytest.fixture()
def masked(tmp_path):
    """An editor confined to ``valinor``, with two masks over columns they may
    still name in an action: ``founder`` redacted (which changes the column's
    Arrow type) and ``pop`` nulled (which does not). Both kinds, because the
    check compares the policy's own output and a mask that preserves the type
    is the one a value comparison could miss."""
    ws = Workspace.init(tmp_path / "masked", name="masked")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", founded_cities())
    (ws.ontology_dir / "o.yml").write_text(MASKED_ONTOLOGY)
    ontology = load_ontology(ws.ontology_dir)

    store.set_dataset_policy("cities", {
        "dataset": "cities",
        "row_policy": {"column": "realm", "rules": [
            {"subject_kind": "user", "subject": "elf", "values": ["valinor"]},
        ]},
        "column_masks": [
            {"column": "founder", "mode": "redact", "exempt": []},
            {"column": "pop", "mode": "null", "exempt": []},
        ],
    }, ticket=_TICKET)
    perms = PermissionService(store)
    elf = User(id="e", username="elf", role=Role.editor)

    admin_svc = OntologyService(ws, catalog, store, ontology)
    elf_svc = OntologyService(
        ws, catalog, store, ontology,
        policy=perms.query_policy_fn(elf),
        policy_for=perms.per_dataset_policy_fn(elf),
        plan_for=perms.arrow_policy_fn(elf),
        decide_for=lambda ds, cols, _u=elf: perms.decide(ds, cols, _u),
    )
    return admin_svc, elf_svc


def test_the_masks_are_actually_on(masked):
    """The premise every test below rests on. If this ever stops holding, the
    refusals become assertions about nothing."""
    _admin, elf = masked
    city = elf.get("city", "city-0")
    assert city["founder"] == "***" and city["pop"] is None


def test_an_update_cannot_write_through_a_column_mask(masked):
    """The mask said "you may not read this cell". Writing it was still
    allowed, and the overlay is applied *after* the policied scan — so the
    author read their own value back in plaintext and the cell was unmasked for
    as long as the edit lived. It is also a blind overwrite: a writeback folds
    it over a value the author was never shown, for everyone."""
    admin, elf = masked
    admin.reindex("city")

    with pytest.raises(ValueError, match="masked for you"):
        elf.apply_action("appoint", pk="city-0", parameters={"founder": "me"})

    assert admin.store.max_edit_seq("city") == 0, "refused, not silently dropped"
    assert admin.get("city", "city-0")["founder"] == "founder-0"
    assert elf.get("city", "city-0")["founder"] == "***", "still masked"


def test_a_type_preserving_mask_is_caught_too(masked):
    """``null`` masking keeps the column's Arrow type, so nothing about the
    schema betrays it — only the value does."""
    admin, elf = masked
    with pytest.raises(ValueError, match="masked for you"):
        elf.apply_action("recount", pk="city-0", parameters={"pop": 999})
    assert admin.get("city", "city-0")["pop"] == 100
    assert admin.store.max_edit_seq("city") == 0


def test_the_refusal_names_every_masked_property_and_a_remedy(masked):
    """"Denied" is not a message anyone can act on. The caller has to learn
    which properties are the problem and what they can do instead."""
    _admin, elf = masked
    with pytest.raises(ValueError) as exc:
        elf.apply_action("refound", pk="city-0",
                         parameters={"founder": "me", "pop": 5})
    message = str(exc.value)
    assert "'founder'" in message and "'pop'" in message
    assert "are masked for you" in message
    assert "mask exemption" in message


def test_an_update_cannot_move_a_row_out_of_the_policy(masked):
    """The mirror image of the create channel: the row filter runs on the
    *base* value, so the merged row was never re-checked. An editor confined to
    valinor could push an object into beleriand — another tenant's partition —
    and keep reading it, and a fold made that the dataset's own truth."""
    admin, elf = masked
    admin.reindex("city")

    with pytest.raises(ValueError, match="outside the rows"):
        elf.apply_action("rename", pk="city-0", parameters={"realm": "beleriand"})

    assert admin.store.max_edit_seq("city") == 0
    assert admin.get("city", "city-0")["realm"] == "valinor"


def test_the_escape_was_visible_to_its_author_before_the_fix(masked):
    """Names the second consequence separately from the first, so a partial
    fix cannot pass: even if the write were somehow acceptable, the object must
    not still be readable by the person who moved it out of their own set."""
    admin, elf = masked
    try:
        elf.apply_action("rename", pk="city-0", parameters={"realm": "beleriand"})
    except ValueError:
        pass
    assert elf.get("city", "city-0")["realm"] == "valinor"
    assert {o["__pk"] for o in elf.query("city", limit=20)["objects"]} == {
        "city-0", "city-2", "city-4"}
    assert admin.query("city", limit=20)["total"] == 6


def test_a_legitimate_update_still_works(masked):
    """Fail closed, not fail useless. An unmasked property, set to a value
    inside the caller's own partition, is exactly what the feature is for."""
    admin, elf = masked
    admin.reindex("city")
    elf.apply_action("rename", pk="city-0", parameters={"realm": "valinor"})
    assert admin.store.max_edit_seq("city") == 1
    assert elf.get("city", "city-0")["realm"] == "valinor"


def test_a_policied_delete_is_not_refused(masked):
    """A delete removes a row the caller can already see in full and produces
    no merged row to re-check. Refusing it would be a feature removal wearing a
    security fix's clothes."""
    admin, elf = masked
    admin.reindex("city")
    elf.apply_action("raze", pk="city-0", parameters={})
    assert elf.get("city", "city-0") is None
    assert admin.get("city", "city-0") is None


def test_an_unpoliced_update_writes_every_column(masked):
    """The refusal is scoped to callers a dataset policy narrows. An admin has
    no mask to write through and no row set to fall out of."""
    admin, _elf = masked
    admin.reindex("city")
    admin.apply_action("refound", pk="city-1",
                       parameters={"founder": "admin", "pop": 7})
    admin.apply_action("rename", pk="city-1", parameters={"realm": "anywhere"})
    city = admin.get("city", "city-1")
    assert city["founder"] == "admin" and city["pop"] == 7
    assert city["realm"] == "anywhere"


def test_the_check_never_hands_the_caller_the_unmasked_row(masked):
    """The whole difficulty of this fix: it needs the base row the policy has
    already withheld. It reads it under a system identity — the same trick
    `reindex` uses — so the value has to stay inside the check. If a refusal
    ever quoted it, the fix would *be* the disclosure."""
    admin, elf = masked
    admin.reindex("city")
    messages = []
    for action, params in [("appoint", {"founder": "me"}),
                           ("recount", {"pop": 5}),
                           ("rename", {"realm": "beleriand"})]:
        try:
            elf.apply_action(action, pk="city-0", parameters=params)
        except ValueError as exc:
            messages.append(str(exc))
    assert len(messages) == 3
    for message in messages:
        assert "founder-0" not in message, "the masked value leaked in an error"
        assert "100" not in message, "the masked population leaked in an error"


def test_the_store_never_holds_a_value_the_author_could_not_read(masked):
    """The durable half. The materialization is shared, so a value written
    through a mask does not merely mislead its author — it replaces the real
    one for every reader of the store and, after a fold, of the dataset."""
    admin, elf = masked
    admin.reindex("city")
    for action, params in [("appoint", {"founder": "me"}), ("recount", {"pop": 1})]:
        with pytest.raises(ValueError):
            elf.apply_action(action, pk="city-0", parameters=params)
    row = admin.store.object_index_rows("city", ["city-0"])[0]
    import json
    props = json.loads(row["props_json"])
    assert props["founder"] == "founder-0" and props["pop"] == 100


def test_the_refusal_reaches_the_front_door_as_a_400_with_its_message(tmp_path):
    """Through HTTP, because a check the real dependency graph does not wire up
    is not a check. It also proves the message survives to the UI: the action
    form renders the response detail in an ErrorBox, so this string is what an
    editor actually reads."""
    from fastapi.testclient import TestClient

    from laurelin.api import create_app

    ws = Workspace.init(tmp_path / "http", name="http")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("cities", founded_cities())
    (ws.ontology_dir / "o.yml").write_text(MASKED_ONTOLOGY)

    app = create_app(ws)
    admin = TestClient(app)
    creds = {"username": "root", "password": "trustno1!"}
    assert admin.post("/api/v1/auth/setup", json=creds).status_code == 200
    assert admin.post("/api/v1/auth/login", json=creds).status_code == 200
    assert admin.post("/api/v1/users", json={
        "username": "elf", "password": "password123", "role": "editor"}).status_code == 200
    assert admin.put("/api/v1/datasets/cities/policy", json={
        "row_policy": {"column": "realm", "rules": [
            {"subject_kind": "user", "subject": "elf", "values": ["valinor"]}]},
        "column_masks": [{"column": "founder", "mode": "redact", "exempt": []}],
    }).status_code == 200

    elf = TestClient(app)
    assert elf.post("/api/v1/auth/login", json={
        "username": "elf", "password": "password123"}).status_code == 200

    masked = elf.post("/api/v1/ontology/actions/appoint/apply",
                      json={"pk": "city-0", "parameters": {"founder": "me"}})
    assert masked.status_code == 400
    detail = masked.json()["detail"]
    assert "masked for you" in detail and "mask exemption" in detail
    assert "founder-0" not in detail, "the value the mask hides must not be in the error"

    escaping = elf.post("/api/v1/ontology/actions/rename/apply",
                        json={"pk": "city-0", "parameters": {"realm": "beleriand"}})
    assert escaping.status_code == 400
    assert "outside the rows" in escaping.json()["detail"]

    # …and the object is untouched, for the editor and for everyone else.
    assert elf.get("/api/v1/ontology/objects/city/city-0").json()["realm"] == "valinor"
    assert admin.get("/api/v1/ontology/objects/city/city-0").json()["founder"] == "founder-0"

    # The legitimate edit through the same door still lands.
    assert elf.post("/api/v1/ontology/actions/rename/apply",
                    json={"pk": "city-0", "parameters": {"realm": "valinor"}}
                    ).status_code == 200


# ------------------------------------------- second round: around the update guard
#
# `_refuse_policy_escaping_update` above was attacked. It holds; what it does
# not cover is every other way to reach the same outcome.

def test_a_delete_does_not_free_a_key_for_a_policied_create(masked):
    """**The whole update guard, walked around for the price of one edit.**

    ``_refuse_shadowing_create`` asked ``_system_view().get()``, which applies
    the overlay — including the caller's own pending delete. So ``raze city-0``
    then ``found city-0`` found no existing object and was accepted.

    A delete is a pending edit, not a fact: the base row is still in the
    dataset, the fold applies last-wins, and the create therefore *replaces*
    it. Measured end to end: an editor whose ``rename`` to ``beleriand`` was
    correctly refused reached exactly that state with ``raze`` + ``found``.
    """
    admin, elf = masked
    admin.reindex("city")
    with pytest.raises(ValueError, match="outside the rows"):
        elf.apply_action("rename", pk="city-0", parameters={"realm": "beleriand"})

    elf.apply_action("raze", pk="city-0", parameters={})
    with pytest.raises(ValueError, match="already exists"):
        elf.apply_action("found", pk=None, parameters={
            "name": "city-0", "realm": "beleriand", "pop": 999, "founder": "me"})

    # The delete stands — it removes a row the caller could already see in
    # full, which is why deletes are not policy-checked. What must not have
    # happened is the *replacement*: the elf's values for the two masked
    # columns must exist nowhere, and the base row they would have overwritten
    # is still intact behind the pending delete.
    assert admin.get("city", "city-0") is None, "the elf's own delete is legitimate"
    assert not any(o.get("founder") == "me"
                   for o in admin.query("city", limit=50)["objects"])
    base = admin.catalog.read("cities").to_pylist()
    row = next(r for r in base if r["name"] == "city-0")
    assert row == {"name": "city-0", "realm": "valinor", "pop": 100,
                   "founder": "founder-0"}


def test_the_delete_then_create_refusal_says_why_the_key_is_still_taken(masked):
    """A refusal an operator reads as "but I just deleted it" is a bug report.
    The message has to name the pending delete and the writeback."""
    _admin, elf = masked
    elf.apply_action("raze", pk="city-0", parameters={})
    with pytest.raises(ValueError) as caught:
        elf.apply_action("found", pk=None, parameters={
            "name": "city-0", "realm": "valinor"})
    message = str(caught.value)
    assert "pending delete" in message and "written back" in message


def test_a_create_may_not_land_outside_the_rows_the_policy_allows(masked):
    """``_policy_admits`` is the *read* half and was doing its job: the create
    is hidden from its author. Hidden from the author is not unwritten — the
    edit is recorded and ``reindex`` materializes it into the **shared** store
    under a system identity, where the tenant it landed on reads it.

    So the question the read half asks is now asked before the write.
    """
    admin, elf = masked
    with pytest.raises(ValueError, match="outside the rows"):
        elf.apply_action("found", pk=None, parameters={
            "name": "city-new", "realm": "beleriand", "pop": 1, "founder": "me"})

    admin.reindex("city")
    assert admin.get("city", "city-new") is None
    assert "city-new" not in {o["__pk"] for o in admin.query("city", limit=50)["objects"]}


def test_a_create_inside_the_policy_is_still_accepted(masked):
    """The cost of the rule above, bounded: a policied editor must still be
    able to create. Masks are deliberately *not* checked on a create — writing
    ``founder`` on a brand new object overwrites nothing and discloses nothing,
    and refusing it would stop a policied editor creating anything at all on a
    dataset carrying any mask."""
    admin, elf = masked
    elf.apply_action("found", pk=None, parameters={
        "name": "city-new", "realm": "valinor", "pop": 7, "founder": "me"})

    admin.reindex("city")
    assert admin.get("city", "city-new")["realm"] == "valinor"
    assert elf.get("city", "city-new") is not None


def test_an_update_may_not_move_an_object_to_a_different_primary_key(masked):
    """``rekey`` walks straight through ``_refuse_policy_escaping_update``: the
    key is unmasked so nothing renders differently, and the row filter is on a
    different column so the merged row still passes. The create path refuses
    this exact collision; the update path had no equivalent.

    Refused for *every* caller, not only policied ones — the writeback
    corruption below needs no policy at all.
    """
    admin, elf = masked
    with pytest.raises(ValueError, match="primary key"):
        elf.apply_action("rekey", pk="city-0", parameters={"name": "city-1"})
    # city-1 is a beleriand row the elf cannot see, and it is untouched.
    assert admin.get("city", "city-1")["realm"] == "beleriand"
    assert admin.get("city", "city-0")["realm"] == "valinor"
    # Even onto a key nobody holds: an edit filed under one key producing an
    # object under another orphans it — the editor can then address it by
    # neither name.
    with pytest.raises(ValueError, match="primary key"):
        elf.apply_action("rekey", pk="city-0", parameters={"name": "city-0b"})
    # And for an unpoliced admin too.
    with pytest.raises(ValueError, match="primary key"):
        admin.apply_action("rekey", pk="city-2", parameters={"name": "city-zzz"})


def test_restating_an_objects_own_primary_key_is_not_a_rewrite(masked):
    """The boundary. An action that names the key column and sets it to what it
    already is has changed nothing, and refusing it would be a rule about
    spelling rather than about identity."""
    _admin, elf = masked
    elf.apply_action("rekey", pk="city-0", parameters={"name": "city-0"})
    assert elf.get("city", "city-0") is not None


def test_a_writeback_never_publishes_a_dataset_whose_key_is_not_unique(tmp_path):
    """``_refuse_duplicate_keys`` counts duplicates in ``__base`` — the dataset
    *before* the fold. The overlay is what introduces one, so the guard passed,
    the fold published a dataset with a non-unique primary key, and every later
    writeback then failed the input check on a duplicate this code had written
    itself. The docstring's own remedy was the only way out of a state no
    operator caused.

    Checked on the fold's *output*, which is the invariant rather than the
    route: a fold publishes a dataset, and it must demand of what it writes
    what it demands of what it read.
    """
    ws = Workspace.init(tmp_path / "dupes", name="dupes")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", founded_cities())
    (ws.ontology_dir / "o.yml").write_text(MASKED_ONTOLOGY)
    service = OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))

    ot = service.ontology.object_type("city")
    duplicated = pa.table({
        "name": ["city-1", "city-1"],
        "realm": ["valinor", "beleriand"],
        "pop": pa.array([1, 2], type=pa.int64()),
        "founder": ["a", "b"],
    })
    with pytest.raises(ValueError, match="not unique"):
        service._refuse_duplicate_keys_in_result(duplicated, ot, "name")

    # And it is wired into the fold, before anything is published.
    #
    # Stated plainly: with `_refuse_primary_key_rewrite` in place there is no
    # longer a reachable way to make the overlay produce a duplicate, so this
    # half is defence in depth and is asserted as such — the guard runs on the
    # fold's *output*, and it runs before `catalog.write`. Asserting it through
    # an exploit would mean keeping the exploit open.
    seen = {}
    original = type(service)._refuse_duplicate_keys_in_result

    def spy(table, object_type, pk):
        seen["rows"] = table.num_rows
        seen["version_at_call"] = store.get_dataset("cities").latest_version
        return original(table, object_type, pk)

    service._refuse_duplicate_keys_in_result = spy
    service.apply_action("found", pk=None, parameters={
        "name": "city-6", "realm": "valinor", "pop": 6, "founder": "f"})
    result = service.writeback("city", actor="admin")

    assert seen, "the fold must run the output guard"
    assert seen["rows"] == 7, "on the folded table, not on the base"
    assert seen["version_at_call"] == 1, "before the new version is published"
    # A clean fold still publishes, so the guard is not simply refusing.
    assert result["folded"] == 1
    names = catalog.read("cities").column("name").to_pylist()
    assert len(names) == len(set(names))


def test_a_policied_editor_cannot_insert_into_another_tenants_partition(tmp_path):
    """The outcome all of the above is about, asserted from the victim's side.

    Two editors, disjoint realms. Whatever the first one does — create over a
    key they cannot see, create outside their rows, delete then create — the
    second one's object list must not grow, and no object of theirs may change.
    """
    ws = Workspace.init(tmp_path / "tenants", name="tenants")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", founded_cities())
    (ws.ontology_dir / "o.yml").write_text(MASKED_ONTOLOGY)
    ontology = load_ontology(ws.ontology_dir)
    store.set_dataset_policy("cities", {
        "dataset": "cities",
        "row_policy": {"column": "realm", "rules": [
            {"subject_kind": "user", "subject": "elf", "values": ["valinor"]},
            {"subject_kind": "user", "subject": "man", "values": ["beleriand"]},
        ]},
        "column_masks": [{"column": "founder", "mode": "redact", "exempt": []}],
    }, ticket=_TICKET)
    perms = PermissionService(store)

    def view(username):
        user = User(id=username, username=username, role=Role.editor)
        return OntologyService(
            ws, catalog, store, ontology,
            policy=perms.query_policy_fn(user),
            policy_for=perms.per_dataset_policy_fn(user),
            plan_for=perms.arrow_policy_fn(user),
            decide_for=lambda ds, cols, _u=user: perms.decide(ds, cols, _u),
        )

    elf, man = view("elf"), view("man")
    before = sorted(o["__pk"] for o in man.query("city", limit=50)["objects"])

    for attempt in (
        lambda: elf.apply_action("found", pk=None, parameters={
            "name": "city-9", "realm": "beleriand", "pop": 9, "founder": "me"}),
        lambda: elf.apply_action("rekey", pk="city-0", parameters={"name": "city-1"}),
    ):
        with pytest.raises(ValueError):
            attempt()

    elf.apply_action("raze", pk="city-0", parameters={})
    with pytest.raises(ValueError):
        elf.apply_action("found", pk=None, parameters={
            "name": "city-0", "realm": "beleriand", "pop": 999, "founder": "me"})

    assert sorted(o["__pk"] for o in man.query("city", limit=50)["objects"]) == before
    assert man.get("city", "city-1")["realm"] == "beleriand"


def test_the_create_refusals_reach_the_front_door_as_400s(tmp_path):
    """Through HTTP, for the same reason the update refusals are: a guard the
    real dependency graph does not wire up is not a guard, and the message is
    what the editor reads in the action form's ErrorBox."""
    from fastapi.testclient import TestClient

    from laurelin.api import create_app

    ws = Workspace.init(tmp_path / "httpc", name="httpc")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("cities", founded_cities())
    (ws.ontology_dir / "o.yml").write_text(MASKED_ONTOLOGY)

    app = create_app(ws)
    admin = TestClient(app)
    creds = {"username": "root", "password": "trustno1!"}
    admin.post("/api/v1/auth/setup", json=creds)
    admin.post("/api/v1/auth/login", json=creds)
    admin.post("/api/v1/users", json={
        "username": "elf", "password": "password123", "role": "editor"})
    assert admin.put("/api/v1/datasets/cities/policy", json={
        "row_policy": {"column": "realm", "rules": [
            {"subject_kind": "user", "subject": "elf", "values": ["valinor"]}]},
        "column_masks": [{"column": "founder", "mode": "redact", "exempt": []}],
    }).status_code == 200

    elf = TestClient(app)
    elf.post("/api/v1/auth/login", json={"username": "elf", "password": "password123"})

    assert elf.post("/api/v1/ontology/actions/raze/apply",
                    json={"pk": "city-0", "parameters": {}}).status_code == 200
    recreated = elf.post("/api/v1/ontology/actions/found/apply", json={
        "parameters": {"name": "city-0", "realm": "beleriand", "pop": 999,
                       "founder": "me"}})
    assert recreated.status_code == 400
    assert "already exists" in recreated.json()["detail"]

    escaping = elf.post("/api/v1/ontology/actions/found/apply", json={
        "parameters": {"name": "city-new", "realm": "beleriand", "pop": 1}})
    assert escaping.status_code == 400
    assert "outside the rows" in escaping.json()["detail"]

    rekeyed = elf.post("/api/v1/ontology/actions/rekey/apply",
                       json={"pk": "city-2", "parameters": {"name": "city-1"}})
    assert rekeyed.status_code == 400
    assert "primary key" in rekeyed.json()["detail"]

    # The object the elf may not see is untouched by any of it.
    assert admin.get("/api/v1/ontology/objects/city/city-1").json()["realm"] == "beleriand"
    # And a legitimate create through the same door still lands.
    assert elf.post("/api/v1/ontology/actions/found/apply", json={
        "parameters": {"name": "city-ok", "realm": "valinor", "pop": 1}}
    ).status_code == 200


# -- the overlay is read through the policy too, not only written through it ---
#
# `_refuse_policy_escaping_update` is the *write* half and was present and
# correct: an editor cannot themselves write a masked column or move a row out
# of their allowlist. These are the *read* half, which did not exist. The
# overlay is merged onto the base rows AFTER the policied scan, so until it is
# policed here, any live edit — including one made by a legitimately exempt
# user or an admin — is read back unpoliced by everybody.

OVERLAY_ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      realm: {type: string}
      ssn: {type: string}
actions:
  - api_name: rename
    object_type: city
    kind: update
    parameters:
      realm: {type: string}
      ssn: {type: string}
  - api_name: found
    object_type: city
    kind: create
    parameters:
      name: {type: string, required: true}
      realm: {type: string, required: true}
      ssn: {type: string}
"""


def _overlay_env(tmp_path, *, row_policy, column_masks):
    ws = Workspace.init(tmp_path / "ovl", name="ovl")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", pa.table({
        "name": [f"city-{i}" for i in range(4)],
        "realm": [["valinor", "beleriand"][i % 2] for i in range(4)],
        "ssn": [f"ssn-{i}" for i in range(4)],
    }))
    (ws.ontology_dir / "o.yml").write_text(OVERLAY_ONTOLOGY)
    ontology = load_ontology(ws.ontology_dir)
    store.set_dataset_policy("cities", {
        "dataset": "cities", "row_policy": row_policy, "column_masks": column_masks,
    }, ticket=_TICKET)
    perms = PermissionService(store)
    elf = User(id="e", username="elf", role=Role.editor)

    def for_user(user):
        return OntologyService(
            ws, catalog, store, ontology,
            policy=perms.query_policy_fn(user),
            policy_for=perms.per_dataset_policy_fn(user),
            plan_for=perms.arrow_policy_fn(user),
            decide_for=lambda ds, cols, _u=user: perms.decide(ds, cols, _u),
        )

    return OntologyService(ws, catalog, store, ontology), for_user(elf)


def test_an_update_to_a_masked_column_is_read_back_masked(tmp_path):
    """Measured end to end: a mask-exempt editor applied ``rename`` setting
    ``ssn``; a second, non-exempt editor then read the plaintext SSN out of
    ``GET /ontology/objects/city/city-0``, out of an aggregate grouped by
    ``ssn``, and by searching a fragment of it — while the same column of the
    same dataset still read ``***`` everywhere else. The mask came back the
    moment the edit was folded into the dataset, which is what proved the mask
    itself was configured correctly and only the overlay path disclosed."""
    admin, elf = _overlay_env(
        tmp_path, row_policy=None,
        column_masks=[{"column": "ssn", "mode": "redact", "exempt": []}],
    )
    assert elf.get("city", "city-0")["ssn"] == "***"
    admin.apply_action("rename", "city-0", {"ssn": "SSN-987-65-4321"}, actor="root")

    assert elf.get("city", "city-0")["ssn"] == "***"
    assert {o["ssn"] for o in elf.query("city", limit=10)["objects"]} == {"***"}
    groups = elf.aggregate("city", group_by=["ssn"])["groups"]
    assert {g["ssn"] for g in groups} == {"***"}
    assert elf.query("city", search="987-65")["objects"] == []
    # The exact path (`_materialize`) is the oracle the other two are checked
    # against, so it has to agree rather than be the hole they route around.
    assert {o["ssn"] for o in elf._materialize(elf.ontology.object_type("city"))} == {
        "***"
    }


def test_a_created_objects_masked_columns_are_read_back_masked(tmp_path):
    """``_policy_admits`` ran the creates through the policy and then returned
    the **raw** payload for whichever keys survived — so the row filter was
    honoured and the column masks were not."""
    admin, elf = _overlay_env(
        tmp_path, row_policy=None,
        column_masks=[{"column": "ssn", "mode": "redact", "exempt": []}],
    )
    admin.apply_action(
        "found", "city-9",
        {"name": "city-9", "realm": "valinor", "ssn": "CREATED-SECRET"}, actor="root",
    )
    assert elf.get("city", "city-9")["ssn"] == "***"
    assert "CREATED-SECRET" not in str(elf.query("city", limit=10))
    assert "CREATED-SECRET" not in str(
        elf._materialize(elf.ontology.object_type("city"))
    )


def test_an_update_that_moves_a_row_out_of_the_allowlist_hides_it(tmp_path):
    """The row filter ran on the *base* value and the overlay rewrote the
    column afterwards, so a user confined to ``realm='valinor'`` kept reading
    an object an admin had moved to ``'beleriand'`` — and could filter for it
    by that value. It disappeared only once the edit was folded, i.e. for
    exactly as long as the hand edit was live."""
    admin, elf = _overlay_env(
        tmp_path,
        row_policy={"column": "realm", "rules": [
            {"subject_kind": "user", "subject": "elf", "values": ["valinor"]}]},
        column_masks=[],
    )
    assert pks(elf.query("city", limit=10)) == {"city-0", "city-2"}
    admin.apply_action("rename", "city-0", {"realm": "beleriand"}, actor="root")

    assert pks(elf.query("city", limit=10)) == {"city-2"}
    assert elf.get("city", "city-0") is None
    assert elf.query("city", filters={"realm": "beleriand"})["objects"] == []
    assert {o["__pk"] for o in elf._materialize(elf.ontology.object_type("city"))} == {
        "city-2"
    }
    # And an update that leaves the row where it is stays visible: the rule is
    # "this assignment moved it out", not "there is an edit".
    admin.apply_action("rename", "city-2", {"realm": "valinor"}, actor="root")
    assert pks(elf.query("city", limit=10)) == {"city-2"}


def test_a_policied_service_that_cannot_resolve_its_policy_refuses_the_overlay(
    tmp_path,
):
    """Fail closed rather than optional. A service constructed with a policy
    but no ``decide_for`` cannot hold the overlay to the same rules as the rows
    it merges onto, and serving it unpoliced in that case is how the fix would
    quietly stop applying on the next call site."""
    admin, elf = _overlay_env(
        tmp_path, row_policy=None,
        column_masks=[{"column": "ssn", "mode": "redact", "exempt": []}],
    )
    admin.apply_action("rename", "city-0", {"ssn": "SSN-987-65-4321"}, actor="root")
    elf.decide_for = None
    with pytest.raises(ValueError, match="decide_for"):
        elf.query("city", limit=10)


NULL_KEY_ONTOLOGY = """
object_types:
  - api_name: city
    backing_dataset: cities
    primary_key: name
    title_property: name
    properties:
      name: {type: string}
      realm: {type: string}
actions:
  - api_name: rekey
    object_type: city
    kind: update
    parameters:
      name: {type: string}
      realm: {type: string}
"""


def test_an_update_may_not_clear_an_objects_primary_key(tmp_path):
    """The null case, which ``_refuse_primary_key_rewrite`` short-circuited as
    "restating the key is not a rewrite". It is neither: the key is present in
    the payload and set to nothing. (An action whose key parameter is
    ``required`` catches it by accident; one where it is optional — which is
    the ordinary way to write a partial-update action — did not.)

    Measured through the live route: a policied editor set the key to null, got
    200, and the object rendered with ``__pk`` of the string ``'None'``,
    findable under neither its old key nor ``'None'`` (the SQL compares
    ``CAST(name AS VARCHAR) = 'None'`` against a NULL column, which is never
    true). There is no API anywhere in ``laurelin/`` to revoke a live object
    edit, so it is unaddressable permanently — and a second null on another row
    wedged ``writeback`` for the whole object type, two NULL keys being
    duplicate keys, with a 400 no operator action could clear.
    """
    ws = Workspace.init(tmp_path / "nk", name="nk")
    store = MetadataStore(ws.metadata_path)
    catalog = DatasetCatalog(ws, store)
    catalog.write("cities", cities())
    (ws.ontology_dir / "o.yml").write_text(NULL_KEY_ONTOLOGY)
    svc = OntologyService(ws, catalog, store, load_ontology(ws.ontology_dir))

    with pytest.raises(ValueError, match="primary key"):
        svc.apply_action("rekey", pk="city-0", parameters={"name": None})
    assert svc.get("city", "city-0") is not None
    assert len(svc.query("city", limit=10)["objects"]) == 6
    # A non-key property may still be cleared; the rule is about identity.
    svc.apply_action("rekey", pk="city-0", parameters={"realm": None})
    assert svc.get("city", "city-0")["realm"] is None
    # And the fold that two null keys would have blocked forever still runs.
    with pytest.raises(ValueError, match="primary key"):
        svc.apply_action("rekey", pk="city-2", parameters={"name": None})
    assert svc.writeback("city", actor="root")["folded"] == 1


def test_the_index_routes_are_gated_per_object_type_like_every_other(tmp_path):
    """``POST`` and ``DELETE /ontology/object-types/{name}/index`` were gated by
    the **global EDITOR role alone** while every other ``/ontology`` route runs
    ``_require_ot_view``/``_require_ot_edit``. Two things went through it:

    * a global editor with *no* grant on the type — 403 on the type and 403 on
      its objects — got a 200 carrying the global object count and the whole
      state block, and could DELETE the owner's index out from under them;
    * a caller with view but a row policy showing them three of six objects got
      ``objects: 6``, the exact number ``get_object_type`` withholds from a
      policied caller ("operator numbers: they go to callers the backing
      dataset's policy does not narrow").
    """
    from fastapi.testclient import TestClient

    from laurelin.api import create_app

    ws = Workspace.init(tmp_path / "idx", name="idx")
    store = MetadataStore(ws.metadata_path)
    DatasetCatalog(ws, store).write("cities", cities())
    (ws.ontology_dir / "o.yml").write_text(ONTOLOGY)

    app = create_app(ws)
    admin = TestClient(app)
    creds = {"username": "root", "password": "trustno1!"}
    admin.post("/api/v1/auth/setup", json=creds)
    admin.post("/api/v1/auth/login", json=creds)
    admin.post("/api/v1/users",
               json={"username": "elf", "password": "password123", "role": "editor"})
    elf = TestClient(app)
    elf.post("/api/v1/auth/login",
             json={"username": "elf", "password": "password123"})

    # (a) no grant on this object type at all.
    assert admin.put("/api/v1/ontology/permissions/city", json={"grants": [
        {"subject_kind": "user", "subject": "root",
         "can_view": True, "can_edit": True}]}).status_code == 200
    # 404 and not 403, on the read AND the write doors: a withheld object type
    # answers exactly like an unknown one, because the list route already omits
    # it and a 403 one URL over handed its existence back. The gate is the
    # same; only what the refusal discloses changed.
    assert elf.get("/api/v1/ontology/object-types/city").status_code == 404
    assert elf.get("/api/v1/ontology/objects/city").status_code == 404
    assert elf.post("/api/v1/ontology/object-types/city/index").status_code == 404
    assert elf.delete("/api/v1/ontology/object-types/city/index").status_code == 404
    # The owner's index is still theirs to build, and still there afterwards.
    assert admin.post("/api/v1/ontology/object-types/city/index").status_code == 200
    assert admin.get(
        "/api/v1/ontology/object-types/city").json()["index"]["indexed"] is True

    # (b) grant restored, but a row policy narrows what elf may see. The
    # rebuild is allowed; the operator counters are not disclosed.
    assert admin.put("/api/v1/ontology/permissions/city", json={"grants": [
        {"subject_kind": "user", "subject": "root",
         "can_view": True, "can_edit": True},
        {"subject_kind": "user", "subject": "elf",
         "can_view": True, "can_edit": True}]}).status_code == 200
    assert admin.put("/api/v1/datasets/cities/policy", json={
        "row_policy": {"column": "realm", "rules": [
            {"subject_kind": "user", "subject": "elf", "values": ["valinor"]}]},
        "column_masks": [],
    }).status_code == 200
    assert admin.put("/api/v1/datasets/cities/permissions", json={"grants": [
        {"subject_kind": "user", "subject": "root",
         "can_view": True, "can_edit": True},
        {"subject_kind": "user", "subject": "elf",
         "can_view": True, "can_edit": True}]}).status_code == 200
    listed = elf.get("/api/v1/ontology/objects/city").json()
    assert listed["total"] == 3
    built = elf.post("/api/v1/ontology/object-types/city/index")
    assert built.status_code == 200
    assert built.json()["objects"] is None
    assert built.json()["state"] is None
    # The unpoliced caller still gets the numbers they are there to operate on.
    assert admin.post("/api/v1/ontology/object-types/city/index").json()["objects"] == 6
