"""Point-of-decision guidance: the paths a new user actually walks, pinned.

An attack pass drove the product as a novice and found that every journey's
first success stranded them: the dataset detail page (where an import lands
you) offered nothing but Upload/Compact/Prev/Next; the Dashboards empty state
sent users to Analyses, which cannot put anything on a dashboard; Explore and
Analyses sat side by side with no word about which to use; and the save
dialog's failure named neither the field nor the fix. These tests pin the
repaired behavior.

Two kinds of test share this file, in the styles this repo already uses:

* ``tests/webapp_harness/ux_guidance.tsx`` renders the REAL components
  through react-dom/server per role, so the role-filtering of the new doors
  is asserted against emitted markup, not against the source table; and
* source scans pin wiring a server render cannot observe (a modal that opens
  on click, a mutation's error path, an effect's shape).

Skips as a unit when node or the webapp's node_modules are absent (a
Python-only checkout); CI and any tree that can build the UI run it.
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
HARNESS = REPO / "tests" / "webapp_harness" / "ux_guidance.tsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the webapp's node_modules are not available",
)


@pytest.fixture(scope="module")
def mounted(tmp_path_factory) -> dict:
    """Bundle the harness against the live source tree and run it once."""
    out = tmp_path_factory.mktemp("ux") / "ux_guidance.cjs"
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
    html = mounted[key] if isinstance(mounted[key], str) else mounted[key]["html"]
    assert not html.startswith("CRASH:"), f"{key} threw at mount:\n{html}"
    return html


# ------------------------------------------------- the dataset next-step doors

def test_the_dataset_detail_page_offers_next_step_doors_for_every_role(mounted):
    # The page a new user lands on right after their first import must not be
    # a dead end: every role gets at least one door onward from the data.
    editor = mounted["open_in_editor"]["hrefs"]
    assert "/explore?dataset=tidy_flights" in editor
    assert "/workbench?dataset=tidy_flights" in editor
    assert "/pipelines?from=tidy_flights" in editor
    assert "/schedules" in editor
    # Editors get no permissions door: revealing restriction-existence to
    # editors is a governance decision the attack passes have not reviewed.
    assert "/admin" not in editor

    admin = mounted["open_in_admin"]["hrefs"]
    assert "/admin" in admin
    for door in editor:
        assert door in admin


def test_a_viewers_dataset_next_steps_never_include_an_editor_door(mounted):
    # Same filter as the nav: a door renders only when the caller's role
    # passes the target's `needs`. A viewer gaining an editor door here would
    # be a role-gating regression, not a style change.
    viewer = mounted["open_in_viewer"]["hrefs"]
    assert viewer == ["/workbench?dataset=tidy_flights"]


def test_explore_opens_on_the_dataset_named_in_the_url(mounted):
    # /explore?dataset=flights (the detail page's door) starts shaping that
    # dataset: its rail entry is the active one, the neighbour's is not.
    html = rendered(mounted, "explore_preset")
    assert 'class="ex-src-item mono active"' in html and ">flights</button>" in html
    # And the active entry is flights, not orders.
    import re
    active = re.search(r'class="ex-src-item mono active"[^>]*>([^<]*)</button>', html)
    assert active and active.group(1) == "flights"


def test_a_new_pipeline_opened_from_a_dataset_starts_with_that_dataset(mounted):
    # /pipelines?from=X opens the naming dialog immediately…
    assert "Name your new pipeline" in rendered(mounted, "pipelines_from")
    # …and a plain open does not.
    assert "Name your new pipeline" not in rendered(mounted, "pipelines_plain")
    # The rest of the chain runs in effects a server render cannot observe,
    # so its wiring is pinned at source: the dialog forwards the dataset to
    # the builder, and the builder seeds the first step with it.
    src = (SRC / "views" / "Flows.tsx").read_text(encoding="utf-8")
    assert "&dataset=${encodeURIComponent(fromDataset)}" in src
    assert "params: { dataset: presetSource }" in src


def test_the_sql_page_prefills_a_starter_query_for_the_dataset_named_in_the_url(mounted):
    # CodeMirror owns the document, so the seed is pinned at source: the
    # ?dataset= door lands one Ctrl+Enter away from rows.
    src = (SRC / "views" / "Workbench.tsx").read_text(encoding="utf-8")
    assert 'searchParams.get("dataset")' in src
    assert "`SELECT * FROM ${presetDataset} LIMIT 100`" in src


# --------------------------------------------------------- dashboards guidance

def test_the_dashboards_empty_state_points_only_at_doors_that_can_reach_a_dashboard(mounted):
    # It used to say "build one by clicking in Analyses" — and Analyses has no
    # dashboard affordance at all, so the user who obeyed the product's own
    # hint built a chart and then stalled. Only Explore and SQL can put a
    # panel on a dashboard today.
    html = rendered(mounted, "dashboards_empty_editor")
    assert 'href="/explore"' in html
    assert 'href="/workbench"' in html
    assert "/analyses" not in html


def test_a_dashboard_page_states_its_zero_step_visibility_to_every_role(mounted):
    # Sharing is zero-step by design; without this line a new user cannot
    # tell whether the dashboard they just made is private or
    # workspace-visible without asking someone.
    sentence = "Visible to everyone in this workspace"
    assert sentence in rendered(mounted, "dashboard_page_editor")
    assert sentence in rendered(mounted, "dashboard_page_viewer")
    # An indicator only — it must not have grown sharing controls.
    for key in ("dashboard_page_editor", "dashboard_page_viewer"):
        assert "Share" not in rendered(mounted, key)


def test_the_add_panel_modal_offers_the_no_code_path_beside_the_sql_wall():
    # The modal opens on click, which a server render cannot do, so the
    # pointer is pinned at source: a NEW panel's SQL form links to Explore
    # with the dashboard prefilled; an EDIT does not (Explore cannot reopen a
    # raw-SQL panel, so pointing an edit there would dead-end).
    src = (SRC / "views" / "Dashboards.tsx").read_text(encoding="utf-8")
    assert "/explore?dashboard=${encodeURIComponent(dashboard)}" in src
    assert "!(initial.sql || initial.object_type) && (" in src


# ----------------------------------------------- the two click-to-chart doors

def test_explore_and_analyses_each_say_which_job_the_other_is_for():
    # Until the Milestone B merge, the two near-identical surfaces sit side
    # by side in the same nav group; the only alternative to this sentence is
    # discovering the difference by building the same chart twice.
    explore = (SRC / "views" / "Explore.tsx").read_text(encoding="utf-8")
    assert 'to="/analyses"' in explore
    assert "one chart for a dashboard" in explore
    analyses = (SRC / "views" / "Analyses.tsx").read_text(encoding="utf-8")
    assert 'to="/explore"' in analyses
    assert "multi-step document" in analyses


# ------------------------------------------------------- honest save failures

def test_the_save_to_dashboard_control_names_the_shaping_issue_instead_of_a_generic_refusal():
    # With an invalid summary name elsewhere on the page, the stale preview
    # kept the chart on screen and Save failed with "The query is not
    # finished yet." — naming neither the field nor the fix. The button now
    # disables with the issue as its tooltip, and the mutation's backstop
    # error carries the same sentence.
    src = (SRC / "views" / "Explore.tsx").read_text(encoding="utf-8")
    assert '(tab === "datasets" && issues.length > 0)' in src
    assert 'title={tab === "datasets" && issues.length > 0 ? issues[0] : undefined}' in src
    assert 'throw new Error(issues[0] ?? "The query is not finished yet.")' in src


# ------------------------------------------------------------------ schedules

def test_schedule_build_targets_are_offered_from_the_known_pipelines_not_typed_from_memory():
    # The pipeline list is known to the server and rendered as a picker
    # everywhere else; here it was a bare text input and a typo tax. Known
    # outputs are now checkboxes; the free-text box stays only for a target
    # that will exist later (authoring a schedule before its pipeline is a
    # supported order — the save-then-warn banner covers it).
    src = (SRC / "views" / "Schedules.tsx").read_text(encoding="utf-8")
    assert "api.get<TransformSummary[]>(`${API}/transforms`)" in src
    assert "knownTargets" in src and 'type="checkbox"' in src
    assert "s.targets.includes(o)" in src


# ----------------------------------------------------------------- vocabulary

def test_the_visual_builders_step_copy_never_calls_a_pipeline_a_flow():
    # vocab.ts is the visual builder's entire user-facing vocabulary, and it
    # is where 'Every flow begins with data you can already read.' hid from
    # the rename sweep (the sweep covered Flows.tsx, not its vocab module).
    import re
    text = (SRC / "views" / "flow" / "vocab.ts").read_text(encoding="utf-8")
    strings = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    offenders = [s for s in strings if re.search(r"\bflows?\b", s, re.IGNORECASE)]
    assert not offenders, f"retired word 'flow' in builder copy: {offenders}"
    assert "Every pipeline begins with data you can already read." in text


# ----------------------------------------------------------------- first run

def test_the_datasets_empty_state_names_the_in_repo_tutorial_path():
    # The tutorial link points at github.com, which is dead on an offline or
    # air-gapped install; the in-repo path keeps the guided path findable
    # until the app serves its own docs.
    src = (SRC / "views" / "Datasets.tsx").read_text(encoding="utf-8")
    assert "docs/tutorials/01-ingest-transform-build.md in the Laurelin" in src
    tutorial = REPO / "docs" / "tutorials" / "01-ingest-transform-build.md"
    assert tutorial.exists(), "the path the empty state names must exist"


# -------------------------------------------------------------- focus hand-off

def test_the_focus_hand_off_waits_for_a_late_heading_instead_of_querying_once():
    # Detail pages render their <h1> after data loads; a single synchronous
    # query found the OLD page's heading (which then unmounted, dropping
    # focus to <body>) or nothing. The effect now parks focus on the main
    # region and hands it to the heading when it appears — and never steals
    # focus from a control the user reached themselves.
    src = (SRC / "Layout.tsx").read_text(encoding="utf-8")
    assert "MutationObserver" in src
    assert "active !== main && active !== document.body" in src
    # The observer must not outlive the page that never renders a heading.
    assert "observer.disconnect()" in src and "clearTimeout" in src
