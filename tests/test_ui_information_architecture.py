"""The navigation is the product's information architecture, pinned.

The UI grew one nav item per workflow until fifteen flat doors sat side by
side, six of them overlapping answers to "make something from data". The IA
consolidation settled five labeled groups with one door per job, and this file
is what keeps it settled: the nav structure, the per-role door lists, the
retired-route redirects and the command palette are all pinned against literal
expected values, so an eleventh workflow that adds a sixteenth door fails CI
until the door is placed in a group here, deliberately.

Two sources of truth are compared:

* ``tests/webapp_harness/ia_mount.tsx`` renders the REAL ``Layout`` through
  react-dom/server once per role and reports the anchors that actually
  appear in the sidebar — so a hardcoded ``<NavLink>`` added beside
  ``NAV_GROUPS`` is caught, not just edits to the array; and
* source scans of ``App.tsx`` pin the redirect and landing wiring that a
  server render cannot observe (``<Navigate>`` fires in an effect, and
  react-dom/server runs none).

Role gating is the part that must never drift: a viewer's nav gaining an item
is a governance regression, not a style change (`needs` semantics per
Layout.tsx). Skips as a unit when node or the webapp's node_modules are absent
(a Python-only checkout); CI and any tree that can build the UI run it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEBAPP = REPO / "laurelin" / "ui" / "webapp"
ESBUILD = WEBAPP / "node_modules" / ".bin" / "esbuild"
HARNESS = REPO / "tests" / "webapp_harness" / "ia_mount.tsx"
APP_TSX = WEBAPP / "src" / "App.tsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the webapp's node_modules are not available",
)

# ----------------------------------------------------------------- the spec
#
# The agreed IA, as literal data. Changing the product's doors means changing
# this table — that is the point.

EXPECTED_GROUPS = [
    {
        "label": "Data",
        "items": [
            {"to": "/datasets", "label": "Datasets"},
            {"to": "/ontology", "label": "Ontology"},
        ],
    },
    {
        "label": "Analyze",
        "items": [
            {"to": "/dashboards", "label": "Dashboards"},
            {"to": "/analyses", "label": "Analyses"},
            {"to": "/explore", "label": "Explore", "needs": "editor"},
            {"to": "/workbench", "label": "SQL"},
            {"to": "/apps", "label": "Apps"},
        ],
    },
    {
        "label": "Build",
        "items": [
            {"to": "/pipelines", "label": "Pipelines", "needs": "editor"},
        ],
    },
    {
        "label": "Operate",
        "items": [
            {"to": "/builds", "label": "Builds"},
            {"to": "/schedules", "label": "Schedules", "needs": "editor"},
            {"to": "/health", "label": "Health"},
        ],
    },
    {
        "label": "Govern",
        "items": [
            {"to": "/audit", "label": "Audit"},
            {"to": "/admin", "label": "Admin", "needs": "admin"},
            {
                "to": "/workspaces",
                "label": "Workspaces",
                "superadmin": True,
                "multiOnly": True,
            },
        ],
    },
]


def expected_visible(role_rank: int, superadmin: bool = False, multi: bool = False):
    """The spec's own role filter, applied to the literal table above."""
    ranks = {"viewer": 0, "editor": 1, "admin": 2}
    out = []
    for g in EXPECTED_GROUPS:
        items = [
            {"to": i["to"], "label": i["label"]}
            for i in g["items"]
            if ranks.get(i.get("needs", "viewer")) <= role_rank
            and (not i.get("superadmin") or superadmin)
            and (not i.get("multiOnly") or multi)
        ]
        if items:
            out.append({"label": g["label"], "items": items})
    return out


ROLE_PARAMS = {
    "viewer": (0, False, False),
    "editor": (1, False, False),
    "admin": (2, False, False),
    "superadmin_single": (2, True, False),
    "superadmin_multi": (2, True, True),
}


@pytest.fixture(scope="module")
def mounted(tmp_path_factory) -> dict:
    """Bundle the harness against the live source tree and run it once."""
    out = tmp_path_factory.mktemp("ia") / "ia_mount.cjs"
    nm = WEBAPP / "node_modules"
    subprocess.run(
        [
            str(ESBUILD), str(HARNESS), "--bundle", "--platform=node",
            "--jsx=automatic", "--loader:.tsx=tsx",
            f"--alias:react={nm / 'react'}",
            f"--alias:react-dom/server={nm / 'react-dom' / 'server'}",
            f"--alias:react/jsx-runtime={nm / 'react' / 'jsx-runtime.js'}",
            f"--alias:react-router-dom={nm / 'react-router-dom'}",
            f"--alias:@tanstack/react-query={nm / '@tanstack' / 'react-query'}",
            f"--outfile={out}",
        ],
        check=True, capture_output=True,
    )
    run = subprocess.run(
        ["node", str(out)], check=True, capture_output=True, text=True,
    )
    return json.loads(run.stdout)


def test_the_nav_is_exactly_the_agreed_five_groups_and_their_items_and_nothing_else(mounted):
    # The source table itself, gates included. A new nav item — or a new gate
    # on an existing one — must be placed here deliberately.
    assert mounted["nav_groups_source"] == EXPECTED_GROUPS
    # And the rendered sidebar agrees with it for the most-privileged caller:
    # every anchor in the <nav> comes from a group block, none ride alongside.
    rendered = mounted["rendered_superadmin_multi"]
    assert rendered["stray_anchors"] == 0, (
        "an anchor rendered in the nav outside the grouped structure — "
        "a door was hardcoded beside NAV_GROUPS"
    )
    assert rendered["groups"] == expected_visible(*ROLE_PARAMS["superadmin_multi"])


@pytest.mark.parametrize("role", list(ROLE_PARAMS))
def test_every_role_sees_exactly_the_agreed_doors_and_a_viewers_nav_never_gains_one(mounted, role):
    expected = expected_visible(*ROLE_PARAMS[role])
    rendered = mounted[f"rendered_{role}"]
    assert rendered["stray_anchors"] == 0
    assert rendered["groups"] == expected, f"{role} sees the wrong doors"
    # The counts the IA settled on: viewer 9, editor 12 (11 once Explore
    # merges into Analyses in the follow-on milestone), admin 13,
    # superadmin 13 single / 14 multi.
    n = sum(len(g["items"]) for g in rendered["groups"])
    assert n == {
        "viewer": 9,
        "editor": 12,
        "admin": 13,
        "superadmin_single": 13,
        "superadmin_multi": 14,
    }[role]


def test_a_nav_group_header_never_renders_when_every_item_in_the_group_is_role_hidden(mounted):
    # A header over nothing advertises hidden capability. For a viewer both
    # Build (Pipelines is editor-gated) and every other emptied group must
    # vanish header and all.
    viewer_groups = [g["label"] for g in mounted["rendered_viewer"]["groups"]]
    assert "Build" not in viewer_groups
    assert viewer_groups == ["Data", "Analyze", "Operate", "Govern"]
    for role in ROLE_PARAMS:
        for g in mounted[f"rendered_{role}"]["groups"]:
            assert g["items"], f"{role}: group {g['label']!r} rendered a header over no items"


def test_every_retired_route_redirects_to_its_replacement(mounted):
    assert mounted["retired_routes"] == {
        "/pipeline": "/builds",
        "/flows": "/pipelines",
        "/transforms": "/pipelines?tab=python",
    }
    # Deep links into the visual builder keep their subpath and search params.
    assert mounted["flows_redirects"] == {
        "bare": "/pipelines",
        "named": "/pipelines/late_orders",
        "named_search": "/pipelines/late_orders?new=1",
    }
    # The wiring: react-dom/server cannot observe <Navigate> (it fires in an
    # effect), so the route table is pinned at source. Each retired path must
    # be routed, and routed to a redirect — not to the old view.
    src = APP_TSX.read_text()
    assert re.search(
        r'<Route\s+path="/pipeline"\s+element=\{<Navigate to=\{RETIRED_ROUTES\["/pipeline"\]\} replace />\}',
        src,
    )
    assert re.search(
        r'<Route\s+path="/transforms"\s+element=\{<Navigate to=\{RETIRED_ROUTES\["/transforms"\]\} replace />\}',
        src,
    )
    assert re.search(r'<Route path="/flows/\*" element=\{<FlowsRedirect />\} />', src)
    assert "flowsRedirectTarget(loc.pathname, loc.search)" in src
    # And the replacements are really routed to the real views.
    assert re.search(r'<Route path="/builds" element=\{scoped\(<BuildsView />\)\} />', src)
    assert re.search(r'<Route path="/pipelines/\*" element=\{scoped\(<PipelinesView />\)\} />', src)


def test_the_landing_route_sends_viewers_to_dashboards_and_authors_to_datasets(mounted):
    # Role-aware landing: a viewer's entry points are things made FOR them.
    # Pinned at source for the same reason as the redirects above.
    src = APP_TSX.read_text()
    assert 'const landing = auth.can("editor") ? "/datasets" : "/dashboards";' in src
    assert re.search(
        r'path="\*"\s+element=\{<Navigate to=\{needsWorkspace \? "/workspaces" : landing\} replace />\}',
        src,
    )


def test_the_command_palette_offers_exactly_the_nav_items_the_callers_role_can_see(mounted):
    for role in ROLE_PARAMS:
        rendered = [
            {"group": g["label"], "label": i["label"], "to": i["to"]}
            for g in mounted[f"rendered_{role}"]["groups"]
            for i in g["items"]
        ]
        assert mounted[f"palette_{role}"] == rendered, (
            f"{role}: the palette and the sidebar disagree — the palette must "
            "consume the same filtered nav, never its own list"
        )
