"""The migration guide is a runbook for an agent, so a sentence in it that
disagrees with the server is a bug of the same rank as a wrong error code.
These tests pin the claims that have already rotted once (or nearly did):
the missing-group-member status code, the Flow IR parameter reference, the
row-policy placement rule, the writeback override flag, and the governance
read-back tools the verification phases lean on.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from laurelin.transforms.flow_ir import (
    AGG_FNS,
    CAST_TYPES,
    NODE_KINDS,
    OPS,
)

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "MIGRATING-FROM-FOUNDRY.md"


@pytest.fixture(scope="module")
def guide() -> str:
    return GUIDE.read_text()


def test_the_guide_documents_the_missing_member_status_as_400_not_404(guide):
    """set_group_members returns 400 for an unknown member (only an unknown
    GROUP is 404 — auth_routes). The guide once said 404 in two places, which
    made an agent using the failure table misclassify the documented
    order-of-operations case."""
    assert "a member username that does not exist yet is a **400**" in guide
    assert "400 from `set_group_members` naming a user" in guide
    assert "404 from `set_group_members`" not in guide


def test_the_guides_flow_ir_reference_covers_every_node_kind_and_vocabulary(guide):
    """The guide claims the IR is closed ('ten node kinds'), so it must carry
    the parameter shape for all ten — authoring a join from the old text took
    three error-message round-trips because 6 of 10 kinds had no shapes."""
    # One table row per kind.
    for kind in NODE_KINDS:
        assert re.search(rf"^\| `{kind}` \|", guide, re.M), f"no params row for {kind!r}"
    # The closed vocabularies, verbatim.
    for fn in AGG_FNS:
        assert f"`{fn}`" in guide or fn in guide, f"aggregate fn {fn!r} undocumented"
    for t in CAST_TYPES:
        assert t in guide, f"cast type {t!r} undocumented"
    for op in OPS:
        assert op in guide, f"expression op {op!r} undocumented"
    # The two shapes that cost a round-trip each in measurement.
    assert '"keys": [{"left"' in guide       # join keys are left/right objects
    assert '"mode": "keep"' in guide          # select requires a mode


def test_the_guide_states_the_row_policy_placement_rule(guide):
    """Flow governance refuses row-policied sources unconditionally, so the
    guide's own Phase-4-then-6 order walks an agent into unbuildable flows
    unless the placement rule is stated where policies are authored."""
    assert "row-policy only datasets that no flow reads" in guide.lower() or \
        "row-policy only datasets no flow reads" in guide.lower()
    assert "FlowRefused" in guide


def test_the_guide_documents_the_writeback_override_for_flow_backed_types(guide):
    """§ 4.2 (denormalize into a flow) and § 4.3 (enable_writeback) intersect
    at a 400 the guide once never mentioned; the override flag and its
    next-build-overwrites consequence must be written down."""
    assert "allow_transform_backed" in guide


def test_the_guide_names_only_governance_verification_tools_that_exist(guide):
    """The verification phases used to instruct reads that no MCP tool could
    perform (governance was write-only). Every read-back tool the guide names
    must exist on the built server, and the propagation claim must point at
    list_dataset_markings rather than the write's echo."""
    for tool in (
        "list_dataset_markings", "list_dataset_grants", "list_dataset_policies",
        "list_object_type_grants", "get_user_clearances",
    ):
        assert tool in guide, f"{tool} missing from the guide"
    assert "it reports what propagated" not in guide

    from laurelin.mcp.client import LaurelinClient
    for tool in (
        "list_dataset_markings", "list_dataset_grants", "list_dataset_policies",
        "list_object_type_grants", "get_user_clearances",
    ):
        assert hasattr(LaurelinClient, tool), f"client lacks {tool}"


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp package not installed (pip install laurelin[mcp])",
)
def test_every_tool_the_guides_inventory_names_is_served(tmp_path):
    """§ 6 closes with 'if a tool named here is missing from your tools/list,
    you are connected to an older Laurelin' — so the named inventory must be
    a subset of what build_server actually serves."""
    import asyncio

    from laurelin.mcp import LaurelinClient, build_server

    inventory = re.findall(r"`([a-z_]+)`", GUIDE.read_text().split("## 6.")[1])
    server = build_server(LaurelinClient(token="unused"))
    served = {t.name for t in asyncio.run(server.list_tools())}
    missing = [t for t in set(inventory) - served if t != "tools"]
    assert not missing, f"guide names tools the server does not serve: {missing}"


def test_the_policy_tool_docstring_documents_every_mask_mode_the_server_accepts():
    """Docstrings are part of the product — the agent reads them, not source.
    The set_dataset_policy docstring once omitted 'hash' (which the guide's
    own Phase 6 example uses), steering an agent toward redact when Foundry
    semantics need a stable pseudonym."""
    import laurelin.mcp.server as mcp_server_module

    source = Path(mcp_server_module.__file__).read_text()
    assert '"null"|"redact"|"hash"' in source
