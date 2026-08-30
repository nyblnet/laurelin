"""The Operate screens' structural promises, mounted.

Builds, Schedules and Health each made an operator do detective work: the
build history's expectation failures lived in a tooltip, red cards had to be
expanded one by one to learn which transform failed, nothing anywhere could
link to a specific build, "Run now" was fire-and-forget, and a schedule's
last build was a bare id you retyped by hand. These tests pin the fixes:

* ``/builds?build=<id>`` opens exactly that card (the page's deep link —
  Health, Schedules and version history all point at it);
* expectation failure messages are visible text, never title= prose;
* a collapsed failed card names its failed transform in the header;
* the expander is a real ``<button aria-expanded>`` (keyboard contract);
* transform names on Builds link to their authoring surface, and every
  transform row offers a per-target build;
* Schedules and Health render build ids as ``/builds?build=`` links;
* Health renders the ``schedule run failed`` derivation when the server
  sends it.

``tests/webapp_harness/operate_mount.tsx`` renders the REAL views through
react-dom/server with their queries seeded. A server render runs no effects,
so effect-side behavior — scroll-into-view, and the run-now poll that
converges the row — is pinned at source below the mounted tests.

Skips as a unit when node or the webapp's node_modules are absent (a
Python-only checkout); CI and any tree that can build the UI run it.
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
VIEWS = WEBAPP / "src" / "views"
ESBUILD = WEBAPP / "node_modules" / ".bin" / "esbuild"
HARNESS = REPO / "tests" / "webapp_harness" / "operate_mount.tsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the webapp's node_modules are not available",
)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    """Bundle the harness against the live source tree and run it once."""
    out = tmp_path_factory.mktemp("operate") / "operate_mount.cjs"
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


def markup(rendered: dict, key: str) -> str:
    html = rendered[key]
    assert not html.startswith("CRASH:"), f"{key} threw at mount:\n{html}"
    return html


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------


def test_the_build_named_in_the_url_opens_and_the_others_stay_shut(rendered):
    html = markup(rendered, "builds_deeplink")
    # The deep-linked card is expanded: its task table (and only its) renders.
    assert 'aria-expanded="true"' in html
    assert 'aria-expanded="false"' in html
    # The collapsed build's expectation message must not be anywhere — not as
    # text, not as a tooltip.
    assert "COLLAPSED_CARD_SENTINEL" not in html
    # Without the param, nothing is expanded.
    plain = markup(rendered, "builds_plain")
    assert 'aria-expanded="true"' not in plain


def test_an_expectation_failure_message_is_visible_text_on_the_build_card(rendered):
    html = markup(rendered, "builds_deeplink")
    # The author's sentence is element text on the open card...
    assert "must be unique — 39 row(s) violate it" in html
    # ...and never only a title= tooltip: no title attribute carries it.
    for m in re.finditer(r'title="([^"]*)"', html):
        assert "must be unique" not in m.group(1), (
            "the expectation message is back in a tooltip — F6 regressed"
        )


def test_a_collapsed_failed_card_names_the_transform_that_failed(rendered):
    plain = markup(rendered, "builds_plain")
    # Every card is collapsed here, so the name can only come from the header.
    assert "load_raw" in plain
    assert "failed:" in plain


def test_the_build_expander_is_a_real_button(rendered):
    html = markup(rendered, "builds_plain")
    assert re.search(r"<button[^>]*aria-expanded=", html), (
        "the /builds row expander must be a <button aria-expanded> — a "
        "clickable div is mouse-only"
    )


def test_transform_names_on_builds_link_to_their_authoring_surface(rendered):
    html = markup(rendered, "builds_deeplink")
    # A visual pipeline links to its builder page; code links to the Python tab.
    assert 'href="/pipelines/agg_flow"' in html
    assert 'href="/pipelines?tab=python"' in html


def test_a_viewers_builds_page_never_links_into_the_editor_gated_pipelines(rendered):
    """The nav hides /pipelines from a viewer (needs:"editor"), and these
    in-page links reopened the door: identical DOM to the editor's, every
    transform name a live link, each click landing on a bare 403. Same data,
    viewer role: the names render as plain text — the rule the page already
    applies to its Build buttons."""
    html = markup(rendered, "builds_viewer")
    assert "agg_flow" in html and "enrich" in html, "the names themselves must stay"
    assert 'href="/pipelines' not in html
    # And the role-hidden build controls stay hidden, as before.
    assert "Build now" not in html


def test_every_transform_row_offers_a_per_target_build(rendered):
    html = markup(rendered, "builds_deeplink")
    assert 'aria-label="Build agg_out"' in html
    assert 'aria-label="Build enriched"' in html


def test_per_target_builds_post_targets_not_always_everything():
    # The wire shape, pinned at source: the mutation forwards `targets` when
    # given (the server accepted this all along; only the scheduler used it).
    src = (VIEWS / "Pipeline.tsx").read_text(encoding="utf-8")
    assert re.search(r"targets\s*&&\s*targets\.length\s*>\s*0\s*\?\s*\{\s*targets\s*\}", src)


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def test_a_schedule_row_links_its_last_build(rendered):
    html = markup(rendered, "schedules")
    assert 'href="/builds?build=bld-1234567890ab"' in html


def test_run_now_acknowledges_and_the_row_converges():
    # Effect-side, so pinned at source: firing Run now (a) announces a receipt
    # in a live region, (b) polls the list while the triggered run is pending,
    # and (c) swaps the receipt for the outcome when the row moves.
    src = (VIEWS / "Schedules.tsx").read_text(encoding="utf-8")
    assert "refetchInterval: watching ? 2000 : false" in src
    assert "requested — the row" in src, "the acknowledgment sentence is gone"
    assert src.count("<LiveStatus") >= 2, (
        "acknowledgment and outcome must both announce via the live region"
    )
    assert "finished:" in src, "the converged outcome sentence is gone"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_renders_the_schedule_run_failed_derivation(rendered):
    html = markup(rendered, "health")
    assert "schedule run failed" in html
    # The dataset is red for a reason the operator can read on the row.
    assert "failing" in html


def test_health_build_ids_are_links_into_the_builds_page():
    # The link renders inside the row's expanded (effect-gated) detail, so it
    # is pinned at source: a build id on Health is a door, not a string to
    # retype.
    src = (VIEWS / "Health.tsx").read_text(encoding="utf-8")
    assert "/builds?build=" in src
