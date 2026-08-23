"""The Explore screen's first render, before any data arrives, mounted for real.

Chip task_e3bb7900 reported two non-fatal "Cannot read properties of
undefined (reading 'map')" errors at Explore's first mount — a child mapping
over a value before its query resolved. The signature matches the already-
fixed raw-SQL-panel crash (Explore.tsx guards `p.flow` with Array.isArray
now), and the live screen no longer reproduces it; this file pins the class
of bug rather than the one instance. It bundles
`tests/webapp_harness/explore_mount.tsx` with the webapp's own esbuild (no
vitest — see test_charts_render.py for why there is no second framework) and
renders the *real* ExploreView through react-dom/server, whose single render
pass with no effects is exactly the pre-data mount: every useQuery pending,
`data` undefined everywhere. An unguarded `.map` anywhere in that render
throws, and the harness records the crash in place of the markup.

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


def test_the_explore_screen_first_mounts_without_throwing_before_any_query_resolves(mounted):
    html = assert_rendered(mounted, "empty")
    # The *editor's* screen rendered — not the viewer's role notice. A wrong
    # auth stub would make every state here pass vacuously on the notice.
    assert "on the left to start" in html
    assert "needs the editor role" not in html


def test_a_resumed_draft_renders_every_picker_before_the_dataset_list_or_schema_arrives(mounted):
    html = assert_rendered(mounted, "draft_datasets")
    # The draft's shaping is on screen while its schema query is still pending.
    assert "average amount" in html
    assert "Summarise" in html


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
