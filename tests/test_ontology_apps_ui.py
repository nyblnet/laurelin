"""The Ontology and Apps surfaces, pinned for someone who never read Foundry docs.

An audit drove these two screens as a novice and found the classic cliffs: an
empty Ontology page said "No object types defined." and nothing else (the
mechanism that fills it — ontology/*.yml — appears nowhere in the product), the
Apps empty state advertised a capability with no door, the object-type cards
and app rows were mouse-only controls, a capped search total rendered as an
exact number, and every keystroke in an object search cost two requests while
unmounting the pager underneath the user.

Two kinds of test share this file, in the styles this repo already uses:

* ``tests/webapp_harness/ontology_apps.tsx`` renders the REAL views through
  react-dom/server, so the empty-state doors and the keyboard markup are
  asserted against emitted HTML, not against the source table; and
* source scans pin wiring a server render cannot observe (retry controls on
  error paths, placeholderData on paged queries, the render-time paging
  reset).

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
SRC = WEBAPP / "src"
ESBUILD = WEBAPP / "node_modules" / ".bin" / "esbuild"
HARNESS = REPO / "tests" / "webapp_harness" / "ontology_apps.tsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the webapp's node_modules are not available",
)


@pytest.fixture(scope="module")
def mounted(tmp_path_factory) -> dict:
    """Bundle the harness against the live source tree and run it once."""
    out = tmp_path_factory.mktemp("ontapps") / "ontology_apps.cjs"
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
    assert not html.startswith("CRASH:"), f"{key} threw at mount:\n{html}"
    return html


# --------------------------------------------------------------- empty states

def test_the_ontology_and_apps_empty_states_name_their_mechanism_and_tutorial(mounted):
    # E4: an empty state that describes a capability must link its mechanism.
    # "No object types defined." named neither the files that define one nor
    # the tutorial that teaches it — a dead end on the exact page where a new
    # team decides whether the ontology is worth setting up.
    ontology = rendered(mounted, "ontology_empty")
    assert "ontology/*.yml" in ontology
    assert "docs/tutorials/02-ontology-and-actions.md" in ontology
    assert (REPO / "docs" / "tutorials" / "02-ontology-and-actions.md").exists(), (
        "the tutorial path the empty state names must exist"
    )

    # Apps: the old copy advertised "an admin can define one" without saying
    # how, and there is no in-app editor. The gate (admin) is disclosable —
    # capability-existence is product documentation — and the mechanism is
    # the REST API, so both are named.
    apps = rendered(mounted, "apps_empty")
    assert "requires an admin" in apps
    assert "PUT /api/v1/apps/" in apps
    assert "docs/ARCHITECTURE.md" in apps
    assert (REPO / "docs" / "ARCHITECTURE.md").exists()


def test_an_object_browser_with_no_rows_names_the_dataset_that_feeds_it(mounted):
    # "No objects match." on a pristine type read as a search problem. The
    # truthful sentence is about plumbing: objects ARE dataset rows, and the
    # dataset that would fill this list is named so the reader knows where to
    # go next.
    html = rendered(mounted, "browser_no_rows")
    assert "tidy_flights" in html
    assert "they appear here as objects" in html


# ------------------------------------------------------------------- keyboard

def test_the_object_type_cards_are_reachable_by_keyboard(mounted):
    # The card grid is the only way into a type. A mouse-only div means a
    # keyboard user can see the ontology exists and cannot open any of it.
    html = rendered(mounted, "ontology_list")
    card = re.search(r'<div[^>]*class="card clickable"[^>]*>', html)
    assert card, "the type card markup moved — update this test"
    assert 'role="button"' in card.group(0)
    assert 'tabindex="0"' in card.group(0)


def test_an_apps_object_rows_follow_the_datatable_keyboard_contract(mounted):
    # The app's object table used to be a raw <table> with onClick rows —
    # cursor:pointer and nothing else. Through DataTable, a clickable row is
    # tabbable and Enter/Space activate it (the contract lives in ui.tsx).
    html = rendered(mounted, "app_page")
    row = re.search(r'<tr[^>]*class="clickable[^"]*"[^>]*>', html)
    assert row, "the app object rows are no longer DataTable clickable rows"
    assert 'tabindex="0"' in row.group(0)


# ------------------------------------------------------------ honest counting

def test_a_capped_object_search_total_renders_as_a_floor_not_an_exact_number(mounted):
    # The server stops counting broad searches at a cap; fmtNum(total) said
    # "10,000" where the truth is "at least 10,000". fmtCount exists for
    # exactly this and renders the plus.
    assert "10,000+" in rendered(mounted, "browser_capped")
    capped_free = rendered(mounted, "browser_exact")
    assert "10,000" in capped_free and "10,000+" not in capped_free


# ------------------------------------------------- wiring a render cannot see

def test_every_auto_run_ontology_and_apps_query_offers_retry_on_failure():
    # App-wide defaults are retry:false, refetchOnWindowFocus:false, so a
    # transient 503 on any of these auto-run surfaces is terminal without a
    # Retry control. Pinned at source: the error path passes onRetry.
    for rel, expected in {
        "views/Ontology.tsx": 2,        # type list + type detail
        "views/Apps.tsx": 4,            # app, type, objects, selected object
        "views/ontology/ObjectBrowser.tsx": 1,
        "views/ontology/ObjectDetail.tsx": 1,
        "views/ontology/LinkSection.tsx": 1,
    }.items():
        text = (SRC / rel).read_text(encoding="utf-8")
        count = text.count("onRetry=")
        assert count >= expected, (
            f"{rel} wires {count} onRetry control(s); expected at least "
            f"{expected}. A failed auto-run query must offer Retry."
        )


def test_the_paged_object_lists_keep_the_previous_page_while_fetching():
    # Without placeholderData the table AND the pager unmount on every page
    # turn and every keystroke — the control the user is clicking vanishes
    # under their pointer. The Explore previews are the shipped model.
    for rel in ("views/ontology/ObjectBrowser.tsx", "views/Apps.tsx"):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert "placeholderData: (prev) => prev" in text, (
            f"{rel} no longer keeps the previous page during a fetch"
        )


def test_a_search_reset_repages_before_the_query_fires_not_after():
    # Resetting offset in an effect (or in onChange, racing the debounce) let
    # a query keyed on the new search and the OLD offset fire first: two
    # requests per keystroke and a flash of the wrong page. The render-time
    # adjust-state-on-change pattern re-renders before anything fetches.
    for rel in ("views/ontology/ObjectBrowser.tsx", "views/Apps.tsx"):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert "setSearchApplied" in text and "setOffset(0)" in text, (
            f"{rel} lost the render-time paging reset"
        )
        assert not re.search(
            r"useEffect\(\(\) => \{\s*setOffset\(0\)", text
        ), f"{rel} reset paging in an effect again (double-query regression)"


def test_a_broken_ontology_definition_page_names_the_mechanism_beside_the_error():
    # One bad edit to ontology/*.yml 500s this page on every request. The bare
    # error names neither the mechanism nor the fix; the page must say where
    # the definitions live and that the parse error is in the server log.
    text = (SRC / "views" / "Ontology.tsx").read_text(encoding="utf-8")
    assert "The object-type definitions could not be loaded" in text
    assert "ontology/*.yml" in text
