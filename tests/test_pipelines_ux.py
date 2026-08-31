"""The Pipelines cluster's UX contracts, pinned.

The build cluster's blockers and high cliffs from the UX audits, each one a
sentence here:

* a corrupt flow file used to strand the author on an infinite spinner with a
  list card promising "open it to see what to fix" (Flows.tsx's `!draft`
  spinner was reachable with `flow: null, error: ...` resolved);
* every save 409 rendered the workspace-broken banner, so "pick another name"
  was announced as "nothing can be saved or built in this workspace";
* the first-run hero on the Visual tab claimed an empty workspace when the
  workspace was full of Python pipelines;
* re-selecting the already-open file on the Python tab blanked the editor and
  the next Save wrote that emptiness over the real file;
* "Build started — watch it on Builds" was a static sentence that outlived
  the build's failure.

Mounted states render the REAL FlowsView through react-dom/server with a
seeded QueryClient (the test_explore_mount.py pattern: one render pass, no
effects, so a seeded query is a resolved query). Source scans follow
test_ui_consistency.py. Skips as a unit when node or the webapp's
node_modules are absent (a Python-only checkout).
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


def _code(*parts: str) -> str:
    """Source with comments removed.

    Several contracts here are about the ABSENCE of a phrase, and the comment
    that records why the phrase was removed necessarily contains it. Scanning
    raw bytes made those assertions unwritable; test_ui_consistency.py solved
    this the same way.
    """
    text = (SRC.joinpath(*parts)).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)(?:^|(?<=\s))//.*$", " ", text)
ESBUILD = WEBAPP / "node_modules" / ".bin" / "esbuild"
HARNESS = REPO / "tests" / "webapp_harness" / "pipelines_mount.tsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the webapp's node_modules are not available",
)


@pytest.fixture(scope="module")
def mounted(tmp_path_factory) -> dict:
    """Bundle the harness against the live source tree and run it once."""
    out = tmp_path_factory.mktemp("pipelines") / "pipelines_mount.cjs"
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


# ------------------------------------------------------------- corrupt flow

def test_a_broken_pipeline_page_shows_its_error_and_a_delete_door_not_a_spinner(mounted):
    html = assert_rendered(mounted, "broken")
    # The server's parse diagnostic is on screen…
    assert "not valid pipeline JSON" in html
    # …with the one repair the screen can offer…
    assert "Delete this pipeline" in html
    # …and no spinner pretending the page is still on its way.
    assert 'class="loading"' not in html


# ----------------------------------------------------------------- 409 split

def test_a_name_collision_409_is_told_apart_from_a_broken_workspace(mounted):
    kinds = mounted["conflicts"]
    assert kinds["json_collision"] == "name_collision"
    assert kinds["json_workspace"] == "workspace_collect_failed"
    # The route's two collision sentences, exactly as write_flow words them
    # today, classify as naming problems even before the server grows its
    # machine-readable {code} discriminator.
    assert kinds["legacy_output_owned"] == "name_collision"
    assert kinds["legacy_code_transform"] == "name_collision"
    # Anything else stays what it always was.
    assert kinds["legacy_collect_failed"] == "workspace_collect_failed"


def test_the_collision_banner_offers_a_rename_not_the_workspace_broken_sentence():
    # The builder renders two different banners off `saveConflict(...)`: the
    # collision one must offer the on-screen fix (save under a new name) and
    # must not be the workspace-broken sentence.
    src = (SRC / "views" / "Flows.tsx").read_text(encoding="utf-8")
    assert 'conflict?.kind === "name_collision"' in src
    assert "Save these steps under a different name" in src
    assert 'conflict?.kind === "workspace_collect_failed"' in src


def test_the_naming_dialog_refuses_a_dataset_owned_name_before_the_server_has_to():
    # A pipeline's name is the name of the dataset it builds; a name a dataset
    # already owns is a guaranteed 409, so the dialog refuses it with the rule.
    #
    # DELIBERATE edit: the dialog moved from inside Flows.tsx to
    # views/flow/NameDialog.tsx so the Python tab could reuse it instead of
    # calling window.prompt. The invariant is unchanged and now covers both
    # tabs; the Visual tab must still be the one that PASSES the dataset names,
    # because only it names a dataset.
    dlg = (SRC / "views" / "flow" / "NameDialog.tsx").read_text(encoding="utf-8")
    assert "takenDatasets" in dlg
    assert "There is already a dataset called" in dlg
    assert "takenDatasets=" in (SRC / "views" / "Flows.tsx").read_text(encoding="utf-8")


def test_neither_pipelines_tab_asks_for_a_name_with_a_native_browser_dialog():
    # One screen, two tabs, two dialog systems: the Visual tab shipped a styled
    # NameDialog carrying a comment about why window.prompt is wrong for this
    # job, and the Python tab called window.prompt anyway — so whether you were
    # told the naming rule, or that the name was taken, before you committed to
    # it depended on which tab you happened to be standing on. A prompt cannot
    # show a rule, cannot validate as you type, and cannot be announced.
    for name in ("Flows.tsx", "Transforms.tsx"):
        src = _code("views", name)
        assert "window.prompt" not in src, f"{name} still collects a name with window.prompt"
        assert "window.alert" not in src, f"{name} still reports a refusal with window.alert"
    assert "NameDialog" in _code("views", "Transforms.tsx")


def test_the_python_tab_names_a_file_and_the_visual_tab_names_a_pipeline():
    # A .py file declares SEVERAL pipelines — which is exactly why the Python
    # tab's delete confirm says "the datasets" in the plural. Calling the file
    # a "pipeline" would be false, so the two tabs use two nouns on purpose and
    # the shared header no longer flattens them into "two ways to write it".
    tf = _code("views", "Transforms.tsx")
    assert "+ New pipeline file" in tf
    assert 'noun="pipeline file"' in tf
    subtitle = _code("views", "Pipelines.tsx")
    assert "One kind of thing; written one at a time, " in subtitle
    assert "or several to a file" in subtitle
    assert "two ways to write it" not in subtitle


# ------------------------------------------------------------ first-run hero

def test_the_first_run_hero_yields_to_a_python_tab_pointer_when_python_pipelines_exist(mounted):
    html = assert_rendered(mounted, "list_python_only")
    assert "Python tab" in html
    assert "?tab=python" in html
    # The hero's claim belongs only to a truly empty workspace.
    assert "Build a pipeline without writing code." not in html
    assert "Start your first pipeline" not in html


def test_the_first_run_hero_still_greets_a_truly_empty_workspace(mounted):
    html = assert_rendered(mounted, "list_first_run")
    assert "Build a pipeline without writing code." in html
    assert "Start your first pipeline" in html


# ----------------------------------------------------- build follow-through

def test_the_flow_builder_follows_its_build_to_an_outcome():
    # "Build started" was a static sentence; the builder now polls the build
    # it started (the Builds page's refetchInterval pattern) and converges the
    # note to succeeded/failed, linking the build it is talking about.
    src = _code("views", "Flows.tsx")
    assert "refetchInterval" in src
    assert "/builds?build=" in src
    # DELIBERATE edit: the outcome copy joined the one sentence family every
    # build-kicking screen now uses ("Build … finished: <outcome>." +
    # "See the build" — the Builds page's own phrasing, which Schedules
    # already copies). The invariant here is unchanged: the note converges to
    # a terminal outcome and links the build it is talking about.
    # DELIBERATE edit (second): the terminal fact converged completely across
    # the three build-kicking screens — "Build <id> finished: <outcome>." then
    # "See the build." and nothing after it. The trailing clauses each screen
    # had invented ("— <dataset> has N rows", "for what went wrong") are what
    # let three voices grow, so their ABSENCE is now part of the contract.
    assert "finished: {buildQ.data.status}." in src
    assert "See the build" in src
    assert "for what went wrong" not in src
    assert "Build finished:" not in src
    # The outcome is announced to screen readers, not just repainted.
    assert "LiveStatus" in src


# ------------------------------------------------------------- python tab

def test_reselecting_the_open_file_does_not_blank_the_editor():
    # openFile used to setDoc("") unconditionally; with contentQ's cached data
    # unchanged the reload effect never re-fired, and Save then wrote the
    # blank buffer over the real file. Re-selecting the open file is a no-op.
    src = (SRC / "views" / "Transforms.tsx").read_text(encoding="utf-8")
    assert "buffer.name === info.name) return;" in src


def test_the_two_tabs_introduce_one_product_not_two():
    # Both tab index screens render the SAME header sentence (the shared
    # PIPELINES_SUBTITLE), so the Visual and Python surfaces read as two
    # idioms of one thing rather than two products sharing a nav item.
    flows = (SRC / "views" / "Flows.tsx").read_text(encoding="utf-8")
    transforms = (SRC / "views" / "Transforms.tsx").read_text(encoding="utf-8")
    pipelines = (SRC / "views" / "Pipelines.tsx").read_text(encoding="utf-8")
    assert "export const PIPELINES_SUBTITLE" in pipelines
    assert "subtitle={PIPELINES_SUBTITLE}" in flows
    assert "subtitle={PIPELINES_SUBTITLE}" in transforms


def test_the_python_lock_notice_points_at_analyses_not_the_retired_explore_door():
    # /explore merged into Analyses; the lock notice's no-code alternatives
    # must name doors that exist.
    src = (SRC / "views" / "Transforms.tsx").read_text(encoding="utf-8")
    assert '"/explore"' not in src
    assert 'to="/analyses"' in src
