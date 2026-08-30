"""The hand-rolled SVG charts and the shared shaping model, rendered for real.

A chart is a claim about measured values, so a misleading chart is a
correctness bug, not a cosmetic one — and none of these claims are reachable
from Python. There is no frontend test framework in the webapp (vitest was
deliberately not introduced; the single-file zero-CDN bundle is the product),
so this file bundles `tests/webapp_harness/harness.tsx` with the webapp's own
esbuild, renders the *real* `charts.tsx` and `views/shaping/model.ts` (the
one model behind the quick chart and Analyses cells) through react-dom/server
under node, and asserts on the exact markup — the same way the defects here
were originally demonstrated.

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
ESBUILD = WEBAPP / "node_modules" / ".bin" / "esbuild"
HARNESS = REPO / "tests" / "webapp_harness" / "harness.tsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the webapp's node_modules are not available",
)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    """Bundle the harness against the live source tree and run it once."""
    out = tmp_path_factory.mktemp("charts") / "harness.cjs"
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


def x_axis_labels(svg: str) -> list[str]:
    """The x-axis tick texts, in document order. Charts are 640x260 with a
    34px bottom pad, so the axis text row sits at y=242 — a constant the
    component owns and this helper mirrors."""
    return re.findall(r'y="242"[^>]*>([^<]*)</text>', svg)


def rects(svg: str) -> list[tuple[float, float]]:
    """(x, width) of every bar, in document order (series-major)."""
    return [
        (float(m.group(1)), float(m.group(2)))
        for m in re.finditer(r'<rect x="([0-9.]+)" y="[0-9.]+" width="([0-9.]+)"', svg)
    ]


# ---------------------------------------------------------------------------
# NULL is not zero
# ---------------------------------------------------------------------------


def test_a_null_value_in_a_line_series_renders_as_a_gap_not_a_zero(rendered):
    svg = rendered["line_null"]
    # No point ever claims "Mar · revenue: 0", and the line breaks into two
    # segments instead of plunging to the baseline.
    assert "Mar · revenue" not in svg
    assert svg.count("<polyline") == 2
    assert "1 missing value shown as gap" in svg


def test_a_null_bar_is_not_drawn_as_a_measured_zero(rendered):
    svg = rendered["bar_null"]
    assert "ops · headcount" not in svg  # no bar, no tooltip asserting 0
    assert "eng · headcount: 40" in svg  # the real bars still speak
    assert "missing value shown as gap" in svg


def test_binding_inference_scans_beyond_the_first_row(rendered):
    assert json.loads(rendered["infer_first_null"]) == {
        "xCol": "dept", "yCols": ["headcount"],
    }


def test_scatter_reports_how_many_rows_were_not_drawn(rendered):
    svg = rendered["scatter_skip"]
    assert svg.count("<circle") == 3
    assert "2 rows with a missing value not drawn" in svg


# ---------------------------------------------------------------------------
# Order and position tell the truth
# ---------------------------------------------------------------------------


def test_series_pivot_orders_x_labels_by_each_series_relative_order(rendered):
    """Rows sorted (series, x) once rendered Jan, Mar, Feb — a strictly
    rising series drawn as a peak-and-decline. The pivot now merges each
    series' own order topologically."""
    labels = x_axis_labels(rendered["pivot_order"])
    assert labels == ["Jan", "Feb", "Mar"]


def test_numeric_bins_occupy_a_numeric_axis_so_empty_bins_are_visible(rendered):
    """Bins 0, 30, 60, 300: eight empty bins' worth of gap must not render
    identically to one bin's width. The empty positions are drawn as empty
    axis width — never as fabricated zero marks."""
    svg = rendered["histogram_gap"]
    labels = x_axis_labels(svg)
    assert labels == [str(v) for v in range(0, 301, 30)]
    assert len(rects(svg)) == 4  # only the measured bins draw bars
    assert "missing value" not in svg  # an empty bin is not a "missing value"


def test_scatter_axes_fit_the_data_rather_than_forcing_zero(rendered):
    """Five distinct points clustered far from zero once spanned 0.77px."""
    xs = [float(m) for m in re.findall(r'cx="([0-9.]+)"', rendered["scatter_cluster"])]
    assert len(xs) == 5
    assert max(xs) - min(xs) > 300  # spread across the plot, not a blob


def test_an_unordered_categorical_bar_gets_a_stable_value_descending_order(rendered):
    """A GROUP BY with no ORDER BY hands the chart the engine's arbitrary
    order, and the same dashboard panel drew its categories shuffled
    differently across refreshes. With no order stated by the data, bars now
    sort first-measure-descending — the pie's largest-first rule
    generalized."""
    labels = x_axis_labels(rendered["bar_unordered"])
    assert labels == ["east", "south", "west", "north"]


def test_a_deliberate_order_in_the_data_survives_the_default_sort(rendered):
    # A monotonic measure is an ORDER BY the user chose: kept verbatim.
    assert x_axis_labels(rendered["bar_ordered_kept"]) == ["carrots", "apples", "bananas"]
    # And `keepOrder` asserts deliberate order the heuristic cannot see.
    assert x_axis_labels(rendered["bar_keeporder"]) == ["west", "east", "north", "south"]


def test_grouped_bars_never_overflow_their_group_band(rendered):
    """8 series × 40 groups: the old 2px floor made adjacent groups' bars
    physically interleave, attaching bars to the wrong x label."""
    all_rects = rects(rendered["grouped_band"])
    assert len(all_rects) == 320
    pad_left, group_w, n_groups = 52.0, 576.0 / 40, 40
    for idx, (x, w) in enumerate(all_rects):
        group = idx % n_groups  # series-major document order
        lo = pad_left + group_w * group
        hi = lo + group_w
        assert lo - 1e-6 <= x and x + w <= hi + 1e-6, (idx, x, w, lo, hi)


# ---------------------------------------------------------------------------
# Numbers format truthfully
# ---------------------------------------------------------------------------


def test_a_stat_panel_never_rounds_a_nonzero_value_to_zero(rendered):
    svg = rendered["stat_small"]
    assert "0.00400" in svg
    assert ">0<" not in svg


def test_a_stat_of_a_multirow_result_declares_it_shows_one_of_n(rendered):
    # A stat is a claim that the result IS one number. Given a per-region
    # result it can only show the first row — silently, that reads as the
    # total. The badge is mandatory, and a single-row stat never carries it.
    html = rendered["stat_multirow"]
    assert "first of 3 rows — filter to one row, or use a chart" in html
    assert "first of" not in rendered["stat_small"]


def test_axis_tick_labels_are_distinct_for_sub_hundredth_domains(rendered):
    svg = rendered["ticks_small"]
    for label in ("0.001", "0.002", "0.003", "0.004"):
        assert f">{label}<" in svg
    assert ">0.00<" not in svg  # four distinct gridlines all labeled 0.00


def test_billion_scale_ticks_use_a_billions_suffix(rendered):
    svg = rendered["billions"]
    assert ">2B<" in svg
    assert "M<" not in svg  # no more "2000.0M"


# ---------------------------------------------------------------------------
# Labels stay identifiable
# ---------------------------------------------------------------------------


def test_midnight_timestamps_label_as_their_date_not_a_truncated_timestamp(rendered):
    labels = x_axis_labels(rendered["midnight_labels"])
    assert labels == ["2026-01-01", "2026-02-01"]


def test_truncated_x_labels_stay_distinguishable(rendered):
    labels = x_axis_labels(rendered["trunc_labels"])
    assert len(set(labels)) == 2, labels  # not two copies of "customer_g…"


def test_a_series_value_colliding_with_the_x_column_does_not_corrupt_labels(rendered):
    assert x_axis_labels(rendered["series_collision"]) == ["Jan", "Feb"]


def test_scatter_tooltips_name_the_category_of_each_point(rendered):
    assert "north · " in rendered["scatter_label"]
    assert "south · " in rendered["scatter_label"]


# ---------------------------------------------------------------------------
# Pies
# ---------------------------------------------------------------------------


def test_adjacent_pie_slices_never_share_a_color(rendered):
    fills = re.findall(r'fill="(var\([^)]*\))"', rendered["pie_seven"])
    assert len(fills) == 7
    assert fills[-1] != fills[0]   # closing slice touches the first at 12:00
    assert fills[-1] != fills[-2]  # and its other neighbour


def test_a_pie_of_many_categories_folds_the_tail_into_other(rendered):
    svg = rendered["pie_many"]
    assert svg.count("<path") <= 12
    assert "other (19 categories)" in svg


# ---------------------------------------------------------------------------
# Shared-axis honesty
# ---------------------------------------------------------------------------


def test_mismatched_measure_scales_get_a_visible_note(rendered):
    assert "nearly invisible on this shared axis" in rendered["scale_note"]
    assert "row count" in rendered["scale_note"]


# ---------------------------------------------------------------------------
# The shared shaping model (quick chart + Analyses cells)
# ---------------------------------------------------------------------------


def test_a_saved_sql_panels_empty_flow_parses_to_null_instead_of_crashing(rendered):
    """`DashboardPanel.flow` defaults to `{}`, so every SQL panel carries a
    truthy empty flow. `stateFromFlow({})` once threw on `.nodes.map` and the
    whole app unmounted to a blank page."""
    assert rendered["sql_panel_flow"] == "null"


def test_a_text_date_column_synthesizes_the_compilers_cast_step_and_round_trips(rendered):
    """'Average delay by month' on a dataset whose timestamps arrived as text:
    the group's `parse` flag becomes a cast-to-timestamp node — the compiler's
    own step, one compilation path — and reopening the saved flow reconstructs
    the same screen state."""
    flow = json.loads(rendered["cast_flow"])
    kinds = [n["kind"] for n in flow["nodes"]]
    assert kinds == ["source", "cast", "derive", "aggregate", "sort"]
    cast = flow["nodes"][1]
    assert cast["params"] == {"column": "when", "to": "timestamp"}
    derive = flow["nodes"][2]
    assert derive["params"]["expr"]["op"] == "date_trunc"

    state = json.loads(rendered["cast_roundtrip"])
    assert state is not None
    assert state["groups"] == [
        {"column": "when", "bucket": "month", "binWidth": "", "parse": True}]


def test_duplicate_group_columns_refuse_in_plain_english_before_preview(rendered):
    issues = json.loads(rendered["dup_group_issues"])
    assert any("already grouping" in i for i in issues), issues
    assert not any("tep '" in i for i in issues)  # no step vocabulary


def test_a_parenthesized_summary_name_refuses_in_plain_english_before_preview(rendered):
    issues = json.loads(rendered["paren_alias_issues"])
    assert any("letters, digits, underscores and spaces" in i for i in issues), issues
    assert any("Avg delay (min)" in i for i in issues)


def test_a_server_refusal_is_rewritten_into_card_vocabulary(rendered):
    msg = rendered["refusal_rewrite"]
    assert "Summarise card" in msg
    assert "tep '" not in msg  # no "Step 's3'" survives


def test_an_explore_draft_survives_a_serialize_parse_round_trip(rendered):
    assert json.loads(rendered["draft_roundtrip"]) == json.loads(rendered["draft_original"])
    assert json.loads(rendered["draft_garbage"]) == [None, None, None]


def test_picking_a_date_bucket_defaults_the_sort_to_chronological(rendered):
    """A time series nobody ordered charts in whatever order the engine
    grouped it — shuffled months that look like a valid chart. The shared
    transition (views/shaping/model.ts, so Analyses cells inherit it too)
    defaults the sort to the new bucket, ascending, the moment a date bucket
    is picked — unless the author's own sort still names a real column."""
    assert json.loads(rendered["bucket_autosort"]) == {"column": "when month", "dir": "asc"}
    assert json.loads(rendered["bucket_autosort_kept"]) == {"column": "row count", "dir": "desc"}


def test_sort_direction_defaults_by_what_the_column_is(rendered):
    # A measure descends (biggest first is what "sort by the count" means);
    # a grouping ascends (a time column sorted largest-first runs backwards).
    assert json.loads(rendered["sort_measure_default"]) == {"column": "row count", "dir": "desc"}
    assert json.loads(rendered["sort_group_default"]) == {"column": "when", "dir": "asc"}


def test_value_suggestions_are_an_ordinary_flow_over_the_one_preview_route(rendered):
    """No second endpoint, no raw-distinct API: the filter's value suggestions
    are a group-by-count FlowDef through POST /explore/preview, so a caller's
    suggestions are exactly what their own policy lets them read."""
    flow = json.loads(rendered["distinct_flow"])
    assert [n["kind"] for n in flow["nodes"]] == ["source", "aggregate", "sort"]
    assert flow["nodes"][1]["params"]["group_by"] == ["region"]
