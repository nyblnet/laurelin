"""UI consistency guards: a test beats a style guide.

Nine workflows built eighteen views, and the drift was measurable: three
private copies of the same "note" CSS, hex colours pasted into views, empty
states hand-rolled beside a perfectly good EmptyState primitive, and four
different words for one concept. These tests pin the cleaned-up state so the
drift cannot come back — a contributor who never reads the UX spec is stopped
by CI instead.

Everything here reads source as BYTES first (the control-byte test explains
why), and strips comments before scanning copy, so a code name in a comment
("Workbench.tsx") never trips a vocabulary rule.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "laurelin" / "ui" / "webapp" / "src"
VIEWS = SRC / "views"


def view_sources() -> list[Path]:
    files = sorted(VIEWS.rglob("*.ts")) + sorted(VIEWS.rglob("*.tsx"))
    assert len(files) >= 18, "the views directory moved — update this test"
    return files


def strip_comments(text: str) -> str:
    """Remove /* … */ blocks and whole-token // line comments.

    `//` is only a comment when preceded by start-of-line or whitespace, so
    `https://…` inside a string survives.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)(?:^|(?<=\s))//.*$", " ", text)
    return text


# ------------------------------------------------------------- raw bytes

def test_no_view_source_contains_a_raw_control_byte_that_makes_grep_treat_it_as_binary():
    # Explore.tsx once carried a literal NUL inside a string ("join a list on
    # a separator no column name can contain"). It worked at runtime — and it
    # made `grep` without -a silently return NOTHING for the whole 1760-line
    # file, so every source sweep skipped Explore without saying so. The
    # allowed control bytes are exactly tab, LF and CR.
    allowed = {0x09, 0x0A, 0x0D}
    for path in view_sources():
        data = path.read_bytes()
        bad = sorted({b for b in data if b < 0x20 and b not in allowed})
        assert not bad, (
            f"{path.relative_to(ROOT)} contains raw control byte(s) {bad}: "
            "write the escape (e.g. \"\\x00\") instead of the byte, or grep "
            "will treat the file as binary and silently skip it"
        )


# --------------------------------------------------------------- colours

COLOR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(")


def test_no_view_contains_a_hex_or_rgba_color_literal_outside_the_token_sheet():
    # Zero allowlist, on purpose. Every tint a view needs exists as a CSS
    # custom property in styles.css :root (--red-tint-bg, --gold-tint-bg, …);
    # a new colour goes there first, with the token named for its job.
    offenders = []
    for path in view_sources():
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            for m in COLOR_LITERAL.finditer(line):
                if "var(--" in line[max(0, m.start() - 5):m.start() + 6]:
                    continue  # e.g. var(--red-tint-bg) is the point
                offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "colour literals belong in styles.css :root as tokens, not in views:\n"
        + "\n".join(offenders)
    )


def test_the_token_sheets_own_color_literal_count_is_pinned():
    # styles.css is where literals are ALLOWED — it defines the tokens — but
    # its count is pinned so a new colour is a deliberate act (add a token,
    # bump this number in the same change), not an accretion.
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    count = len(COLOR_LITERAL.findall(css))
    assert count <= 44, (
        f"styles.css now holds {count} colour literals (pinned at 44). "
        "If you added a token on purpose, update the pin in the same commit."
    )


# ---------------------------------------------------------------- tables

# Raw <table> elements that predate the DataTable primitive. Ratcheted, not
# banned: wholesale conversion is churn without user value, but a NEW raw
# table (or a new one in a converted file) must use DataTable instead.
RAW_TABLE_ALLOWANCE = {
    "views/Analyses.tsx": 1,
    "views/Apps.tsx": 1,
    "views/Dashboards.tsx": 1,
    "views/Datasets.tsx": 2,
    "views/Explore.tsx": 1,
    "views/Health.tsx": 1,
    "views/IcebergManager.tsx": 2,
    "views/Workbench.tsx": 1,
    "views/Workspaces.tsx": 1,
    "views/admin/AlertsSection.tsx": 1,
    "views/admin/ApprovalsSection.tsx": 1,
    "views/admin/DataSecuritySection.tsx": 2,
    "views/admin/DatasetAccessSection.tsx": 1,
    "views/admin/EnginesSection.tsx": 1,
    "views/admin/GroupsSection.tsx": 1,
    "views/admin/MarkingsSection.tsx": 1,
    "views/admin/OntologyAccessSection.tsx": 1,
    "views/admin/PortabilitySection.tsx": 10,
    "views/workspaces/ManageMembers.tsx": 1,
}


def test_the_number_of_raw_table_elements_per_view_never_grows():
    for path in view_sources():
        rel = path.relative_to(SRC).as_posix()
        count = path.read_text(encoding="utf-8").count("<table")
        allowed = RAW_TABLE_ALLOWANCE.get(rel, 0)
        assert count <= allowed, (
            f"{rel} renders {count} raw <table> element(s); its ratchet allows "
            f"{allowed}. New tabular UI uses the DataTable primitive from "
            "ui.tsx. (If you converted tables, lower the ratchet — never "
            "raise it.)"
        )


# ------------------------------------------------------------ vocabulary

# One word per concept. These are the exact phrases the UX spec retired; the
# settled replacements are in the right-hand column of its vocabulary table:
#   "SQL workbench"       -> "the SQL page"   (the screen is named SQL)
#   "notebook"            -> "analysis"       (the multi-cell artifact)
#   "Run build" / "Run a build" -> "Build now" / a "build"
# "flow" and "table" are deliberately NOT scanned mechanically: they collide
# with identifiers ("flowQ", the chart kind "table", Postgres's own noun) and
# are enforced by review instead.
RETIRED_PHRASES = ["sql workbench", "notebook", "run a build", "run build"]


def test_ui_copy_never_uses_a_retired_word_for_a_settled_concept():
    offenders = []
    for path in view_sources() + [SRC / "Layout.tsx", SRC / "App.tsx", SRC / "ui.tsx"]:
        text = strip_comments(path.read_text(encoding="utf-8")).lower()
        for phrase in RETIRED_PHRASES:
            if phrase in text:
                offenders.append(f"{path.relative_to(ROOT)}: {phrase!r}")
    assert not offenders, (
        "retired vocabulary found outside comments (see the UX spec's "
        "one-word-per-concept table):\n" + "\n".join(offenders)
    )


# ----------------------------------------------------------- empty states

def test_no_view_hand_rolls_the_empty_state_css_class():
    # `<div className="empty">` is EmptyState's own rendering. A view that
    # writes it by hand bypasses the primitive it is imitating, and the two
    # then drift (that is exactly how Ontology's empty list diverged).
    offenders = [
        str(p.relative_to(ROOT))
        for p in view_sources()
        if 'className="empty"' in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "use the EmptyState primitive from ui.tsx instead of its CSS class:\n"
        + "\n".join(offenders)
    )


# Inline "No …"/"never" placeholders in a faint/dim wrapper. Some are honest
# non-list placeholders (a toolbar's "No file selected", a cell's "never"),
# so this is a ratchet like the tables one: a NEW bespoke empty message must
# either be one of these pinned cases or go through EmptyState.
BESPOKE_EMPTY = re.compile(
    r'className="(?:[^"]*\b(?:faint|dim)\b[^"]*)"[^>]*>\s*(?:No |[Nn]ever)'
)
BESPOKE_EMPTY_ALLOWANCE = {
    "views/Explore.tsx": 2,
    "views/Schedules.tsx": 1,
    "views/Sources.tsx": 1,
    "views/Transforms.tsx": 1,
    "views/Workbench.tsx": 2,
    "views/admin/AlertsSection.tsx": 1,
    "views/admin/MarkingsSection.tsx": 3,
    "views/admin/PortabilitySection.tsx": 1,
    "views/flow/StepForm.tsx": 1,
    "views/ontology/LinkSection.tsx": 1,
}


def test_bespoke_faint_empty_messages_never_multiply():
    for path in view_sources():
        rel = path.relative_to(SRC).as_posix()
        count = len(BESPOKE_EMPTY.findall(path.read_text(encoding="utf-8")))
        allowed = BESPOKE_EMPTY_ALLOWANCE.get(rel, 0)
        assert count <= allowed, (
            f"{rel} has {count} hand-rolled empty message(s); its ratchet "
            f"allows {allowed}. An empty LIST renders through EmptyState; "
            "only an inline placeholder that is not a list may stay bespoke "
            "(and then the ratchet is raised deliberately, here)."
        )


# ------------------------------------------------------------ page chrome

# Every file that renders a routed screen's chrome starts with PageHeader, so
# every destination announces itself the same way. Pipelines.tsx is absent by
# design: it is a seam that delegates to Flows/Transforms, which are listed.
ROUTED_CHROME_FILES = [
    "views/Datasets.tsx",
    "views/Dashboards.tsx",
    "views/Analyses.tsx",
    "views/Explore.tsx",
    "views/Pipeline.tsx",
    "views/Schedules.tsx",
    "views/Health.tsx",
    "views/Flows.tsx",
    "views/Transforms.tsx",
    "views/Apps.tsx",
    "views/Ontology.tsx",
    "views/Workbench.tsx",
    "views/Audit.tsx",
    "views/Admin.tsx",
    "views/Workspaces.tsx",
]


def test_every_routed_view_renders_the_page_header_primitive():
    for rel in ROUTED_CHROME_FILES:
        path = SRC / rel
        assert path.exists(), f"{rel} moved — update ROUTED_CHROME_FILES"
        text = path.read_text(encoding="utf-8")
        assert "PageHeader" in text, (
            f"{rel} no longer renders PageHeader; every routed screen "
            "announces itself with the same chrome"
        )
