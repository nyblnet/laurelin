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
    # DELIBERATE guard edit (Explore→Analyses merge, structure spec §1.6):
    # the chart door now points at the merged Analyses quick-chart entry.
    # react-dom/server escapes "&" in attributes, so unescape before comparing.
    editor = [h.replace("&amp;", "&") for h in mounted["open_in_editor"]["hrefs"]]
    assert "/analyses?mode=chart&dataset=tidy_flights" in editor
    assert "/workbench?dataset=tidy_flights" in editor
    assert "/pipelines?from=tidy_flights" in editor
    # DELIBERATE guard edit: the doors carry the dataset the reader is
    # looking at — the receiving pages support the param (Schedules opens
    # its editor with the target ticked; Admin prefills the Dataset-access
    # filter and scrolls), and a bare link made the user re-find this
    # dataset from scratch there.
    assert "/schedules?target=tidy_flights" in editor
    # Editors get no permissions door: revealing restriction-existence to
    # editors is a governance decision the attack passes have not reviewed.
    assert not any(h.startswith("/admin") for h in editor)

    admin = [h.replace("&amp;", "&") for h in mounted["open_in_admin"]["hrefs"]]
    assert "/admin?dataset=tidy_flights" in admin
    for door in editor:
        assert door in admin


def test_a_viewers_dataset_next_steps_never_include_an_editor_door(mounted):
    # Same filter as the nav: a door renders only when the caller's role
    # passes the target's `needs`. A viewer gaining an editor door here would
    # be a role-gating regression, not a style change.
    viewer = mounted["open_in_viewer"]["hrefs"]
    assert viewer == ["/workbench?dataset=tidy_flights"]


def test_the_quick_chart_opens_on_the_dataset_named_in_the_url(mounted):
    # /analyses?mode=chart&dataset=flights (the detail page's door, and the
    # target of the /explore redirect) starts shaping that dataset: its rail
    # entry is the active one, the neighbour's is not.
    html = rendered(mounted, "quick_chart_preset")
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
    # It used to say "build one by clicking in Analyses" back when Analyses
    # had no dashboard affordance, then pointed at Explore. Explore has since
    # merged into Analyses as its quick chart, so the one charting door that
    # can put a panel on a dashboard is /analyses?mode=chart — and the old
    # /explore door must be gone from this copy (the route itself survives
    # only as a redirect).
    html = rendered(mounted, "dashboards_empty_editor")
    assert 'href="/analyses?mode=chart"' in html
    assert 'href="/workbench"' in html
    assert "/explore" not in html


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
    # pointer is pinned at source: a NEW panel's SQL form links to the quick
    # chart (Analyses absorbed Explore) with the dashboard prefilled; an EDIT
    # does not (the quick chart cannot reopen a raw-SQL panel, so pointing an
    # edit there would dead-end).
    src = (SRC / "views" / "Dashboards.tsx").read_text(encoding="utf-8")
    assert "/analyses?mode=chart&dashboard=${encodeURIComponent(dashboard)}" in src
    assert "!(initial.sql || initial.object_type) && (" in src


def test_a_flow_panels_edit_reopens_in_the_merged_quick_chart():
    # The ?dashboard=D&panel=P round trip survives the Explore→Analyses merge:
    # a shaped panel's Edit button targets the merged surface, param-for-param.
    src = (SRC / "views" / "Dashboards.tsx").read_text(encoding="utf-8")
    assert (
        "/analyses?mode=chart&dashboard=${encodeURIComponent(dash.name)}"
        "&panel=${encodeURIComponent(p.id)}"
    ) in src
    assert "/explore" not in src  # no /explore links remain anywhere in the file


def test_the_panel_editor_previews_the_sql_and_feeds_the_bindings_from_its_columns():
    # Panel authoring used to be blind: the first sight of the result was the
    # saved panel on the board, and a typo'd Y binding silently dropped the
    # series. The editor now runs the SQL through the same POST /query as
    # every other surface, renders the result inside the modal, and feeds the
    # X/Y/Split pickers from the result's actual columns (free text stays as
    # the never-previewed fallback).
    src = (SRC / "views" / "Dashboards.tsx").read_text(encoding="utf-8")
    assert "api.post<QueryResult>(`${API}/query`" in src
    assert "Run preview" in src
    assert "preview.data?.columns" in src
    # A later edit of the SQL flags the preview as stale instead of lying.
    assert "The SQL changed since this preview" in src


def test_a_panel_delete_asks_before_it_destroys():
    # Parity with the dashboard-level Delete, which has always confirmed: the
    # panel ✕ was the one destructive control in the app with no gate.
    src = (SRC / "views" / "Dashboards.tsx").read_text(encoding="utf-8")
    assert 'window.confirm(`Delete panel "${p.title}"?`)' in src


def test_a_failed_panel_offers_retry_instead_of_a_dead_end():
    # App.tsx pins retry:false and no refetch-on-focus, so without an explicit
    # retry a transient 503 leaves the panel dead until a full-page reload.
    src = (SRC / "views" / "Dashboards.tsx").read_text(encoding="utf-8")
    assert "onRetry={() => q.refetch()}" in src


# ------------------------------------------------- the one click-to-chart door

# The predecessor of these tests — two adjacent screens each explaining which
# job the OTHER was for — was deleted deliberately with the Explore→Analyses
# merge: the reason it existed (two doors) is gone. The landing page's two
# entries ARE the disambiguation now, so what gets pinned is that both
# entries exist and each names its job in one line.

def test_the_analyses_landing_offers_quick_chart_and_document_entries(mounted):
    html = rendered(mounted, "analyses_landing")
    # The zero-commitment entry, with its job…
    assert "Quick chart" in html
    assert "Save to dashboard" in html
    # …and the committed one, with its.
    assert "New analysis" in html
    assert "multi-step document" in html
    # The name gate is stated inline, not enforced silently.
    assert "Lowercase letters, digits" in html


def test_a_viewers_analyses_landing_gains_no_authoring_entry(mounted):
    # Both entries are editor-only; a viewer keeps the list they always had.
    html = rendered(mounted, "analyses_landing_viewer")
    assert "Quick chart" not in html
    assert "New analysis" not in html
    assert "No analyses yet" in html


# ------------------------------------------------------- honest save failures

def test_the_save_to_dashboard_control_names_the_shaping_issue_instead_of_a_generic_refusal():
    # With an invalid summary name elsewhere on the page, the stale preview
    # kept the chart on screen and Save failed with "The query is not
    # finished yet." — naming neither the field nor the fix. The button now
    # disables with the issue as its tooltip, and the mutation's backstop
    # error carries the same sentence. (Lives in the merged quick chart since
    # the Explore→Analyses merge.)
    src = (SRC / "views" / "analyses" / "QuickChart.tsx").read_text(encoding="utf-8")
    assert '(tab !== "datasets" || issues.length === 0)' in src
    assert "disabled={!chartReady}" in src
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


# ------------------------------------------------------- data screens (DATA)

def test_the_datasets_chart_doors_point_at_the_merged_analyses_quick_chart():
    # §1.6 of the merge spec: every "chart it" string points at one door,
    # /analyses?mode=chart. Datasets had the two that used to disagree
    # ("chart it in Analyses" vs "Explore — chart it").
    src = (SRC / "views" / "Datasets.tsx").read_text(encoding="utf-8")
    assert "/analyses?mode=chart&dataset=${ds}" in src
    assert 'to="/analyses?mode=chart"' in src
    assert "/explore" not in src, "no /explore link may survive the merge here"


def test_the_sources_empty_state_tells_an_editor_the_gate_instead_of_a_full_stop(mounted):
    # Disclosure decision (copy spec §2): capability-existence is product
    # documentation, not a secret. The editor is told adding sources needs an
    # admin; the admin is told the action. Neither gets a dead full stop.
    editor = rendered(mounted, "sources_empty_editor")
    assert "adding sources requires an admin" in editor
    admin = rendered(mounted, "sources_empty_admin")
    assert "add one to pull external data" in admin
    assert "requires an admin" not in admin


def test_an_empty_dataset_renders_an_empty_state_not_an_error_box(mounted):
    # A managed dataset with no versions is NEW, not broken: the rows endpoint
    # 404s ("has no versions") and that used to render as a red ErrorBox under
    # "Row preview". F10: a normal state never renders as an error.
    html = rendered(mounted, "dataset_detail_empty")
    assert "No data yet" in html
    assert "error-box" not in html


def test_importing_under_an_existing_name_requires_an_explicit_add_version_confirm():
    # Importing under an existing name silently became "version N+1 of that
    # dataset" — repurposing an identity without saying so. The collision is
    # checked against the already-fetched dataset list, and the action is
    # renamed on the button itself. (Pinned at source: the flow needs a chosen
    # file, which a server render cannot supply.)
    src = (SRC / "views" / "Datasets.tsx").read_text(encoding="utf-8")
    assert "const collided = existingQ.data?.find((d) => d.name === name)" in src
    assert 'Add v${(collided.latest_version ?? 0) + 1} to existing "${name}"' in src
    assert "already" in src and "pick another name" in src


def test_a_dataset_versions_build_id_links_to_its_build():
    # The version history's build id was dead text; it now deep-links to the
    # Builds page's ?build= param so provenance is one click.
    src = (SRC / "views" / "Datasets.tsx").read_text(encoding="utf-8")
    assert "/builds?build=${encodeURIComponent(v.build_id)}" in src


def test_the_ingest_doors_signpost_each_other():
    # "Register an external table" (scan in place, copy nothing) and "Data
    # sources" (copy rows in on each sync) look identical until the data is
    # stale. Each names the other and the distinction up front.
    datasets = (SRC / "views" / "Datasets.tsx").read_text(encoding="utf-8")
    assert 'jumpToSection("data-sources")' in datasets
    assert "scans a table in place, copying nothing" in datasets
    sources = (SRC / "views" / "Sources.tsx").read_text(encoding="utf-8")
    assert 'getElementById("external-table")' in sources
    assert "Each sync copies the rows into a new version" in sources


def test_the_row_preview_pages_without_unmounting_and_offers_retry():
    # L1/L2: the pager keeps the previous page on screen (dimmed) during a
    # fetch instead of unmounting the table under the cursor, and a failed
    # auto-run fetch offers Retry (the app-wide query config never retries).
    src = (SRC / "views" / "Datasets.tsx").read_text(encoding="utf-8")
    row_preview = src[src.index("function RowPreview") :]
    assert "placeholderData: (prev) => prev" in row_preview
    assert "onRetry={() => refetch()}" in row_preview


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


# ----------------------------------------------- honest empties, phantom doors

def test_a_viewers_empty_datasets_page_names_no_door_their_role_lacks(mounted):
    """E2 applied to the Datasets list: the editor's empty state sells the
    import path; the viewer's used to reuse it verbatim — "import a file
    above" above no import section, "chart it" into an editor-gated quick
    chart, under a false "No datasets yet" when datasets exist withheld. The
    viewer's copy claims only what a viewer can do."""
    editor = rendered(mounted, "datasets_list_empty_editor")
    viewer = rendered(mounted, "datasets_list_empty_viewer")
    # The editor keeps the full on-ramp.
    assert "import a file above" in editor
    # The viewer's empty state: honest absence, no phantom doors.
    assert "No datasets you can read" in viewer
    assert "import a file above" not in viewer
    assert "chart it" not in viewer
    assert "No datasets yet" not in viewer


# ------------------------------------------ unbuilt sources, and saying so

def test_a_dataset_with_no_versions_is_offered_but_says_why_it_cannot_be_used(mounted):
    """A dataset that exists and has never been built has nothing to read.
    Every source picker offered it anyway, and picking it fetched a schema and
    a preview that both 404'd — two developer-shaped `Error 404:` lines on one
    screen, for a name the picker itself suggested.

    Hiding it would only move the confusion (the name is visible on Datasets),
    so it stays listed, disabled, and carrying the reason and the fix."""
    html = rendered(mounted, "quick_chart_unbuilt_source")
    assert ">orders</button>" in html, "a built dataset stays plainly pickable"
    # The unbuilt one is still listed…
    assert "newborn" in html
    # …disabled, annotated in the visible label, and giving the next step.
    assert "(no versions yet)" in html
    assert "No versions yet — build or import into this dataset" in html
    unbuilt = html[html.index("newborn") - 400 : html.index("newborn") + 120]
    assert "disabled" in unbuilt


def test_every_source_picker_declines_an_unbuilt_dataset_the_same_way():
    """One rule, three pickers: the quick chart's rail, the SQL page's
    sidebar, and an analysis cell's "Reads from". A rule enforced on one
    surface and not its neighbours is how the reader learns it is arbitrary."""
    for rel in (
        ("views", "analyses", "QuickChart.tsx"),
        ("views", "Workbench.tsx"),
        ("views", "Analyses.tsx"),
    ):
        src = SRC.joinpath(*rel).read_text(encoding="utf-8")
        assert "latest_version == null" in src, f"{rel[-1]}: no unbuilt-source check"
        assert "no versions yet" in src.lower(), f"{rel[-1]}: the reason is not stated"


# ---------------------------------------------- empty vs withheld, on Health

def test_health_tells_an_empty_workspace_apart_from_a_withheld_one(mounted):
    """The inverse of the Datasets empty-state rule, which Health had wrong in
    the other direction: it said "No datasets visible to you." unconditionally,
    so an administrator standing on a brand-new workspace was told they were
    being withheld from. The server sends one unquantified bit — never a count,
    never a name — and the page branches on it."""
    empty = rendered(mounted, "health_empty_nothing_exists")
    withheld = rendered(mounted, "health_empty_others_withheld")
    # Nothing exists: emptiness, with the door that fixes it.
    assert "No datasets yet" in empty
    assert "/datasets" in empty
    assert "cannot read" not in empty and "visible to you" not in empty
    # Something exists and is not this reader's: the withholding sentence,
    # and no number anywhere near it.
    assert "No datasets you can read" in withheld
    assert "No datasets yet" not in withheld


def test_the_health_empty_state_never_quantifies_what_is_withheld():
    """`others_exist` is a boolean by design: a count of hidden datasets on a
    health page is an enumeration oracle, and counts are exactly what the
    withholding boundary is about. Pin the shape so a later "helpful" count
    cannot slip in."""
    src = (SRC / "views" / "Health.tsx").read_text(encoding="utf-8")
    assert "others_exist?: boolean" in src
    assert "boolean | undefined" in src, "the third state — server did not say"


# ------------------------------------------------- the Objects/datasets seam

def test_the_quick_chart_states_the_object_limit_where_the_reader_meets_it():
    """M13: an analysis cell reads datasets, so an object chart cannot become
    one. That limit lived in a `title` on a disabled button — unreachable by
    keyboard (a disabled button never takes focus), invisible on touch, and
    never seen by anyone who does not hover a control that looks broken."""
    src = (SRC / "views" / "analyses" / "QuickChart.tsx").read_text(encoding="utf-8")
    assert "aria-disabled" in src
    assert "An analysis reads datasets, so an object chart can't become one" in src
    # And the page's own promise no longer claims it for every source.
    assert "goes to a dashboard" in src


def test_both_source_tabs_shape_data_with_the_same_four_cards():
    """M14: switching the source tab swapped the vocabulary and silently
    dropped a capability — different card titles, no Group-by explanation, and
    no Order & top N card at all, with nothing saying the ordering was missing
    rather than misplaced. The nouns may differ ("rows" vs "objects", which is
    honest); the card names and the explanations may not."""
    src = (SRC / "views" / "analyses" / "QuickChart.tsx").read_text(encoding="utf-8")
    cards = (SRC / "views" / "shaping" / "ShapingCards.tsx").read_text(encoding="utf-8")
    for title in ('"ex-card-title">Filter<', '"ex-card-title">Group by<', '"ex-card-title">Summarise<'):
        assert title in src, f"the object tab is missing the {title} card"
        assert title in cards, f"the dataset tab is missing the {title} card"
    assert "Narrow the objects" not in src, "a second name for the Filter card"
    # The filter add-button is one sentence shape with one noun swapped.
    assert "+ keep only objects where…" in src
    assert "+ keep only rows where…" in cards
    # The Group-by explanation the dataset tab has always shown.
    assert src.count("No grouping = one summary row over everything.") == 1
    assert "No grouping = one summary row over everything." in cards
    # The capability gap is stated in the card's place, not left as a hole.
    assert "Order &amp; top N" in src
    assert "Ordering and top-N aren't available for object charts yet." in src
