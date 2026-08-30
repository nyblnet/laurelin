"""Govern + SQL + Workspaces surfaces, pinned against mounted markup.

The pass these tests pin (specs: structure §4 A1–A3/W1–W3/K1, copy §1 F9,
§2 E2/E4, §5 L4):

* **Audit honesty (E2)** — an editor's empty audit page says events above
  their read level are cut, instead of claiming nothing was recorded; an
  admin (who reads every row — ``min_read_role`` tops out at admin) gets the
  real absence. No count of withheld rows is disclosed either way.
* **Audit filters (A1)** — since/until/actor/action ride the S4 query-param
  contract AND are applied to the fetched window client-side with the same
  exact-match semantics, so the bar behaves identically before and after the
  server params land. Sign-in noise is collapsed by default, recoverably.
* **Effective access (A3)** — every Dataset-access card can answer "who can
  see this dataset" by invoking the governance-fingerprint route for that
  one dataset, instead of an admin cross-referencing three screens.
* **Admin TOC (A2)** — the long Admin page carries a sticky per-section
  table of contents and a dataset filter over the three per-dataset card
  lists; deep links use search params (hash routing has no second ``#``).
* **Workbench (F9, L4, W3)** — a comment-only Run is refused before the
  POST; the sessionStorage draft parser tolerates garbage; the empty
  sidebar offers a door; the quick-query → analysis-cell handoff exists.
* **Workspaces (K1)** — creating a workspace refreshes auth (activating the
  first workspace); Manage members signposts where accounts are created.

Mounted tests use the ``tests/webapp_harness/govern_sql_mount.tsx`` +
esbuild + react-dom/server pattern this repo already uses; source scans pin
wiring a server render cannot observe. Skips as a unit when node or the
webapp's node_modules are absent (a Python-only checkout).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEBAPP = REPO / "laurelin" / "ui" / "webapp"
SRC = WEBAPP / "src"
ESBUILD = WEBAPP / "node_modules" / ".bin" / "esbuild"
HARNESS = REPO / "tests" / "webapp_harness" / "govern_sql_mount.tsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the webapp's node_modules are not available",
)


@pytest.fixture(scope="module")
def mounted(tmp_path_factory) -> dict:
    """Bundle the harness against the live source tree and run it once."""
    out = tmp_path_factory.mktemp("govern") / "govern_sql.cjs"
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


def rendered(mounted: dict, key: str) -> str:
    html = mounted[key]
    assert isinstance(html, str)
    assert not html.startswith("CRASH:"), f"{key} threw at mount:\n{html}"
    return html


# ------------------------------------------------------------------- audit


def test_the_audit_page_never_claims_absence_it_cannot_see(mounted):
    # E2: "no events recorded" may render only for a caller who could have
    # seen every event had it existed. min_read_role tops out at admin, so
    # the editor gets the withheld sentence and the admin the real absence.
    editor = rendered(mounted, "audit_empty_editor")
    assert "No events you can read" in editor
    assert "not shown here" in editor
    assert "No audit events recorded yet" not in editor

    admin = rendered(mounted, "audit_empty_admin")
    assert "No audit events recorded yet" in admin
    assert "No events you can read" not in admin


def test_the_audit_filter_bar_offers_time_actor_and_action(mounted):
    html = rendered(mounted, "audit_empty_editor")
    for field in ("audit-since", "audit-until", "audit-actor", "audit-action"):
        assert f'id="{field}"' in html, f"filter control {field} missing"


def test_sign_in_noise_is_collapsed_by_default_and_recoverable(mounted):
    html = rendered(mounted, "audit_rows_admin")
    # The governance event is on the first screen; the sign-in churn is not —
    # but its absence is declared and one click away, never silent.
    # The datalist still offers the login actions as filter VALUES; what must
    # not render is a login row in the table — i.e. the action as badge text.
    assert ">dataset_created<" in html
    assert ">login_succeeded<" not in html
    assert ">login_failed<" not in html
    assert "sign-in event" in html
    assert "Show sign-in events" in html


def test_the_audit_query_sends_the_server_filter_params():
    # The S4 contract: since/until/actor/action as query params. The client
    # builds them even while the server side lands, and applies the same
    # exact-match narrowing to the window it gets back.
    text = (SRC / "views" / "Audit.tsx").read_text(encoding="utf-8")
    for param in ('q.set("since"', 'q.set("until"', 'q.set("actor"', 'q.set("action"'):
        assert param in text, f"audit query no longer sends {param}…)"
    assert "matchesAuditFilters" in text


# ------------------------------------------------------------------- admin


def test_the_admin_page_renders_a_toc_naming_every_section(mounted):
    html = rendered(mounted, "admin_page")
    for sid in mounted["admin_sections"]:
        assert f'id="admin-{sid}"' in html, f"section anchor admin-{sid} missing"
    for label in ("Users", "Dataset access", "Row &amp; column security", "Portability"):
        assert label in html
    assert 'id="admin-dataset-filter"' in html, "the dataset card filter is gone"


def test_each_dataset_access_card_offers_effective_access(mounted):
    # A3: the "who can see this dataset" answer lives on the card that
    # controls the dataset — one expander per card, computed, not summarized.
    html = rendered(mounted, "dataset_access")
    assert html.count("Effective access") == 2, (
        "each seeded dataset card offers exactly one Effective access control"
    )


# --------------------------------------------------------------- workbench


def test_a_comment_only_run_is_refused_before_the_post(mounted):
    c = mounted["comment_only"]
    assert c["comment"] is False
    assert c["block"] is False
    assert c["semicolons"] is False
    assert c["comment_then_sql"] is True
    assert c["plain"] is True
    assert c["refusal"] == "Nothing to run — the editor only contains a comment."
    # And the run path actually consults the helper before mutating.
    text = (SRC / "views" / "Workbench.tsx").read_text(encoding="utf-8")
    assert text.index("sqlHasExecutableStatement(text)") < text.index("runMut.mutate(text)")


def test_the_workbench_draft_round_trips_and_degrades_garbage(mounted):
    d = mounted["draft"]
    assert d["key"] == "laurelin.workbench.draft.v1"
    rt = d["roundtrip"]
    assert rt["sql"] == "SELECT * FROM flights"
    assert rt["view"] == "bar"
    assert rt["result"]["row_count"] == 1
    for bad in ("garbage", "wrong_shape", "array", "null_input"):
        assert d[bad] is None, f"{bad} must degrade to null, not crash or half-parse"
    # An unknown chart kind degrades to no preference, keeping the SQL.
    assert d["bad_view_degrades"] == {"sql": "SELECT 1"}


def test_the_workbench_empty_sidebar_offers_the_datasets_door(mounted):
    html = rendered(mounted, "workbench_empty")
    assert "No datasets yet" in html
    assert "/datasets" in html


def test_the_workbench_offers_save_as_analysis_cell():
    # W3/§2.4: the quick-query → analysis handoff, no new server surface.
    text = (SRC / "views" / "Workbench.tsx").read_text(encoding="utf-8")
    assert "/analyses?mode=doc&sql=" in text
    assert "Save as analysis cell" in text


def test_the_workbench_run_has_a_cancel_wired_to_abort():
    # W2: an abandoned query frees its admission slot.
    text = (SRC / "views" / "Workbench.tsx").read_text(encoding="utf-8")
    assert "AbortController" in text
    assert "abortRef.current?.abort()" in text


# -------------------------------------------------------------- workspaces


def test_workspace_create_refreshes_auth():
    # K1: after create, the shell re-probes /auth/status (which activates the
    # first workspace) instead of leaving a stranded superadmin.
    text = (SRC / "views" / "Workspaces.tsx").read_text(encoding="utf-8")
    assert "refreshAuth()" in text


def test_manage_members_signposts_admin_user_creation(mounted):
    html = rendered(mounted, "manage_members")
    assert "#/admin?section=users" in html
    assert "Create the user" in html
