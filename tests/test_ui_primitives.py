"""The shared UI primitives' accessibility and failure-copy contract, mounted.

This UX pass moved several promises into ``ui.tsx`` so eighteen views cannot
each keep a drifting private copy: one Modal (dialog semantics, focus
containment), one DataTable keyboard contract, an ErrorBox that can offer
Retry, a live region for async outcomes, focus-reachable governance prose,
and failure copy that never blames the platform for the pipeline's own code —
and never tells an admin that a stronger role would see more.

``tests/webapp_harness/ui_primitives.tsx`` renders the REAL components
through react-dom/server, so the assertions here are against emitted markup.
A server render runs no effects, so the effect-side behavior of the Modal
(focus in on open, restore on close, Escape, Tab wrap) is pinned at source in
the companion tests below the mounted ones.

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
HARNESS = REPO / "tests" / "webapp_harness" / "ui_primitives.tsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the webapp's node_modules are not available",
)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    """Bundle the harness against the live source tree and run it once."""
    out = tmp_path_factory.mktemp("prims") / "ui_primitives.cjs"
    nm = WEBAPP / "node_modules"
    subprocess.run(
        [
            str(ESBUILD), str(HARNESS), "--bundle", "--platform=node",
            "--jsx=automatic", "--loader:.tsx=tsx",
            f"--alias:react={nm / 'react'}",
            f"--alias:react-dom/server={nm / 'react-dom' / 'server'}",
            f"--alias:react/jsx-runtime={nm / 'react' / 'jsx-runtime.js'}",
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
# Modal
# ---------------------------------------------------------------------------


def test_the_modal_primitive_renders_real_dialog_semantics(rendered):
    html = markup(rendered, "modal")
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-label="Rename dataset"' in html
    # Drop-in chrome: the same classes the hand-rolled call sites used, so a
    # view migrates without a pixel moving.
    assert 'class="modal-backdrop"' in html
    assert 'class="modal"' in html
    # The dialog itself is focusable as the fallback focus target.
    assert 'tabindex="-1"' in html


def test_the_modal_owns_focus_and_escape_at_source():
    # Effects and key handlers do not render server-side; pin the wiring.
    src = (SRC / "ui.tsx").read_text(encoding="utf-8")
    assert "containFocusTab" in src
    assert 'e.key === "Escape"' in src
    # Focus restore: the opener is captured before focus moves in.
    assert "document.activeElement" in src


def test_the_command_palette_shares_the_tab_containment(rendered):
    src = (SRC / "palette.tsx").read_text(encoding="utf-8")
    assert "containFocusTab" in src
    assert 'aria-modal="true"' in src


# ---------------------------------------------------------------------------
# Failure copy
# ---------------------------------------------------------------------------


ROLE_LIE = "This is everything your role is shown"


def test_the_failure_note_fallback_never_tells_an_editor_a_stronger_role_exists(rendered):
    # Keying the fallback on the missing detail_ref alone told a superadmin
    # to go find an editor. Only a role below editor gets the education line.
    for key in ("failure_note_editor_noref", "failure_note_admin_noref"):
        html = markup(rendered, key)
        assert ROLE_LIE not in html, key
        assert "No further detail was recorded for this failure." in html, key
    # A viewer's projection really is narrower; the sentence is true for them.
    assert ROLE_LIE in markup(rendered, "failure_note_viewer_noref")
    # Unknown caller role must not risk the lie either.
    assert ROLE_LIE not in markup(rendered, "failure_note_unknown_noref")
    # And with a detail_ref present, neither fallback renders.
    with_ref = markup(rendered, "failure_note_editor_ref")
    assert ROLE_LIE not in with_ref
    assert "No further detail was recorded" not in with_ref
    assert "err-0123456789ab" in with_ref


def test_a_blocked_downstream_names_its_failed_upstream_and_points_at_no_log(rendered):
    html = markup(rendered, "blocked_note")
    assert "Skipped: its input &#x27;upstream_fail&#x27; failed" in html
    # Nothing here ran, so there is nothing in any log about it.
    assert "server log" not in html
    assert "Fix the failed upstream" in html


def test_the_pipelines_own_code_is_never_attributed_to_laurelin(rendered):
    html = markup(rendered, "transform_note")
    assert "The pipeline&#x27;s code raised" in html
    assert "Laurelin" not in html.replace("Laurelin never stores", "")


# ---------------------------------------------------------------------------
# Title-carried prose is focus-reachable (never mouse-only)
# ---------------------------------------------------------------------------


def test_withheld_markers_are_focusable_and_carry_their_prose_in_aria(rendered):
    for key in ("withheld", "redacted"):
        html = markup(rendered, key)
        assert 'tabindex="0"' in html, key
        assert "aria-label=" in html, key
    # A value that is not withheld renders bare — no phantom affordance.
    assert "tabindex" not in markup(rendered, "redacted_plain")


def test_the_failure_badge_explanation_is_reachable_without_a_mouse(rendered):
    html = markup(rendered, "failure_badge")
    assert 'tabindex="0"' in html
    assert "aria-label=" in html
    assert "refused the credential" in html  # the advice rides the label


# ---------------------------------------------------------------------------
# DataTable keyboard contract
# ---------------------------------------------------------------------------


def test_a_clickable_table_row_is_tabbable_and_a_plain_one_is_not(rendered):
    clickable = markup(rendered, "table_clickable")
    assert clickable.count('tabindex="0"') == 2  # one per row
    assert 'class="clickable"' in clickable
    plain = markup(rendered, "table_plain")
    assert "tabindex" not in plain
    # Enter/Space activation is an event handler — pinned at source.
    src = (SRC / "ui.tsx").read_text(encoding="utf-8")
    assert 'e.key === "Enter" || e.key === " "' in src


# ---------------------------------------------------------------------------
# Feedback primitives
# ---------------------------------------------------------------------------


def test_the_error_box_offers_retry_only_when_a_retry_exists(rendered):
    with_retry = markup(rendered, "errorbox_retry")
    assert ">Retry</button>" in with_retry
    assert ">Retry" not in markup(rendered, "errorbox_plain")


def test_the_live_status_primitive_is_a_status_live_region(rendered):
    assert 'role="status"' in markup(rendered, "live_status")
