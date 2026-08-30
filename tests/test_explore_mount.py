"""The merged Analyses surface's first render, before any data arrives.

The quick chart (formerly the standalone Explore screen) lives on the
Analyses landing page now. This file keeps every invariant the old Explore
mount pinned — crash-free pre-data mount (chip task_e3bb7900's class of
"Cannot read properties of undefined (reading 'map')" flashes), draft resume,
garbage drafts degrading to a fresh screen, edit-mode open before the panel
loads — re-asserted against the merged surface, and adds the merge's own:
the one-release legacy-draft import, the viewer landing gaining no authoring
surface, document-draft restore after a refresh, and the disabled
unsaved-cell chaining hint.

It bundles `tests/webapp_harness/explore_mount.tsx` with the webapp's own
esbuild (no vitest — see test_charts_render.py for why there is no second
framework) and renders the *real* AnalysesView through react-dom/server,
whose single render pass with no effects is exactly the pre-data mount:
every useQuery pending, `data` undefined everywhere. An unguarded `.map`
anywhere in that render throws, and the harness records the crash in place
of the markup.

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
ESBUILD = WEBAPP / "node_modules" / ".bin" / "esbuild"
HARNESS = REPO / "tests" / "webapp_harness" / "explore_mount.tsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the webapp's node_modules are not available",
)


@pytest.fixture(scope="module")
def mounted(tmp_path_factory) -> dict:
    """Bundle the harness against the live source tree and run it once."""
    out = tmp_path_factory.mktemp("explore") / "explore_mount.cjs"
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


def assert_rendered(mounted: dict, key: str) -> str:
    html = mounted[key]
    assert not html.startswith("CRASH:"), f"{key} threw at mount:\n{html}"
    return html


def test_the_merged_landing_first_mounts_without_throwing_before_any_query_resolves(mounted):
    html = assert_rendered(mounted, "empty")
    # The *editor's* landing rendered: the quick chart section is present and
    # waiting for a source. A wrong auth stub would make every state here
    # pass vacuously on the viewer's list-only page.
    assert "Quick chart" in html
    assert "on the left to start" in html


def test_a_viewers_landing_has_the_list_and_no_authoring_surface(mounted):
    # The quick chart is editor-only, behind the page's own role gate — the
    # merge must not hand a viewer a shaping surface the nav never offered.
    html = assert_rendered(mounted, "viewer_landing")
    assert "Quick chart" not in html
    assert "New analysis" not in html


def test_a_resumed_draft_renders_every_picker_before_the_dataset_list_or_schema_arrives(mounted):
    html = assert_rendered(mounted, "draft_datasets")
    # The draft's shaping is on screen while its schema query is still pending.
    assert "average amount" in html
    assert "Summarise" in html


def test_a_pre_merge_explore_draft_is_imported_for_one_release(mounted):
    # An in-flight shaping session under the old laurelin.explore.draft.v1
    # key must survive the release that merged the screens.
    html = assert_rendered(mounted, "legacy_draft")
    assert "average amount" in html


def test_a_resumed_objects_draft_renders_before_the_object_types_arrive(mounted):
    html = assert_rendered(mounted, "draft_objects")
    assert "avg delay" in html


def test_a_garbage_or_stale_draft_degrades_to_a_fresh_screen_instead_of_a_crash(mounted):
    for key in ("draft_garbage", "draft_halfvalid"):
        html = assert_rendered(mounted, key)
        assert "on the left to start" in html


def test_an_edit_mode_open_renders_before_the_panel_it_will_edit_has_loaded(mounted):
    assert_rendered(mounted, "edit_pending")


def test_resolved_source_lists_render_while_the_shaping_pickers_still_have_no_columns(mounted):
    html = assert_rendered(mounted, "lists_resolved")
    # The rail shows one tab's list at a time; this state is on Datasets.
    assert "orders" in html
    assert "flights" in html


def test_unsaved_document_cells_survive_a_refresh_via_the_session_draft(mounted):
    # AnalysisEditor state was useState-only: one refresh destroyed every
    # unsaved cell. Both cells here exist ONLY in the stored doc draft.
    html = assert_rendered(mounted, "doc_draft_restored")
    assert "Filter the orders" in html
    assert "Summarise" in html


def test_an_unsaved_upstream_cell_is_a_disabled_source_option_that_says_what_to_do(mounted):
    # Chaining reads saved cells; an earlier unsaved cell used to simply not
    # appear, making the product's whole reason to have cells discoverable
    # only by noticing an absence.
    html = assert_rendered(mounted, "doc_draft_restored")
    assert "save it to read from it here" in html
    import re
    opt = re.search(r"<option[^>]*>Cell 1 — save it to read from it here</option>", html)
    assert opt and "disabled" in opt.group(0)


def test_the_shared_cards_grey_masked_columns_and_suggest_values(mounted):
    # Phase-A courtesies, now in the ONE card stack so Analyses cells get
    # them too: a masked column is a disabled "(masked for you)" option in
    # the measure picker, and a text filter's value box is wired to a
    # governed-value datalist.
    html = assert_rendered(mounted, "cards_masked_and_suggesting")
    assert "salary (masked for you)" in html
    import re
    opt = re.search(r"<option[^>]*>salary \(masked for you\)</option>", html)
    assert opt and "disabled" in opt.group(0)
    assert 'list="t-vals-region"' in html
    assert '<datalist id="t-vals-region"' in html
