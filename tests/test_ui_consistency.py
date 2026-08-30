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
    "views/Dashboards.tsx": 1,
    "views/Datasets.tsx": 2,
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
#   "Laurelin's own code raised" -> "The pipeline's code raised" (the code
#   that raised is the workspace's own transform — blaming the platform sent
#   authors to file bugs about their own typo; see the failure-copy spec F3)
RETIRED_PHRASES = [
    "sql workbench",
    "notebook",
    "run a build",
    "run build",
    "laurelin's own code raised",
    # The shaping vocabulary settled by the Explore→Analyses merge: one card
    # stack, one word per concept ("+ one row per…", "No particular order").
    "+ group by…",
    "unordered",
    # The aggregate-measure concept is "summary" everywhere the shaping UI
    # speaks; "metric" survived in the Dashboards Objects panel editor and
    # "+ another summary" was the losing form of the add control.
    "+ another summary",
    "add metric",
    # A kicked build converges with one sentence family ("Build … finished:
    # <outcome>." + "See the build"); this was the third link label for it.
    "watch it on builds",
]


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


# ------------------------------------------------------------------ dialogs

# Hand-rolled `.modal-backdrop` divs that predate the Modal primitive in
# ui.tsx (role=dialog, focus-in, Escape, Tab containment, focus restore — the
# hand-rolled ones have none of it). Ratcheted: each view's owner migrates
# their call sites to <Modal> and lowers their entry to zero in the same
# change; a NEW modal must use the primitive. The end state is an empty dict.
MODAL_BACKDROP_ALLOWANCE = {
    # Explore merged into Analyses; the quick chart's dialogs are <Modal> -> 0.
    # Flows migrated (NameDialog + EjectDialog) to the Modal primitive -> 0.
    # Schedules migrated to the Modal primitive in the Operate pass -> 0.
    # Admin, Workbench, GroupsSection, ManageMembers and RenameModal migrated
    # in the govern+sql pass -> 0.
}


def test_no_view_renders_a_modal_backdrop_outside_the_modal_primitive():
    for path in view_sources():
        rel = path.relative_to(SRC).as_posix()
        count = path.read_text(encoding="utf-8").count('className="modal-backdrop"')
        allowed = MODAL_BACKDROP_ALLOWANCE.get(rel, 0)
        assert count <= allowed, (
            f"{rel} renders {count} hand-rolled modal backdrop(s); its ratchet "
            f"allows {allowed}. Dialogs go through the Modal primitive in "
            "ui.tsx, which owns focus, Escape and Tab containment once. (If "
            "you migrated a dialog, lower the ratchet — never raise it.)"
        )


# ---------------------------------------------------------------- keyboard

# A div or card styled `clickable` without keyboard handling is a control only
# a mouse can reach. The contract lives in ui.tsx's DataTable (onRowClick
# implies tabIndex + Enter/Space); anything clickable outside it must be a
# real <button>/<a> or carry tabIndex + onKeyDown itself. Ratcheted at the
# current offenders; their owners convert them and lower the entries.
CLICKABLE_DIV_ALLOWANCE = {
    # Flows' card grid gained role=button + tabIndex + Enter/Space -> 0.
    # Pipeline.tsx's /builds expander became a real <button aria-expanded> -> 0.
}


def test_no_clickable_div_lacks_keyboard_handling():
    pattern = re.compile(r'className="[^"]*\bclickable\b[^"]*"')
    for path in view_sources():
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        offenders = 0
        for m in pattern.finditer(text):
            # A tag that handles keys near the clickable class is compliant;
            # this is a per-tag heuristic over the surrounding 600 chars,
            # deliberately cheap — the ratchet is what stops regressions.
            window = text[max(0, m.start() - 300):m.start() + 300]
            if "onKeyDown" in window and "tabIndex" in window:
                continue
            offenders += 1
        allowed = CLICKABLE_DIV_ALLOWANCE.get(rel, 0)
        assert offenders <= allowed, (
            f"{rel} has {offenders} clickable element(s) without keyboard "
            f"handling; its ratchet allows {allowed}. Use a real button, the "
            "DataTable contract, or add tabIndex + onKeyDown."
        )


# A button whose only content is a glyph (✕, ×, ↑, ↓) is nameless to a screen
# reader. Ratcheted at the current offenders; a new glyph button carries
# aria-label from birth.
GLYPH_BUTTON = re.compile(r"<button\b([^>]*)>\s*([^<>a-zA-Z0-9{}]{1,3})\s*</button>")
GLYPH_BUTTON_ALLOWANCE = {
    # flow/ExprEditor's remove-glyphs carry aria-label now -> 0.
}


def test_glyph_only_buttons_carry_an_accessible_name():
    for path in view_sources():
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        offenders = [
            m.group(2).strip()
            for m in GLYPH_BUTTON.finditer(text)
            if "aria-label" not in m.group(1)
        ]
        allowed = GLYPH_BUTTON_ALLOWANCE.get(rel, 0)
        assert len(offenders) <= allowed, (
            f"{rel} has glyph-only button(s) {offenders} without aria-label; "
            f"its ratchet allows {allowed}."
        )


# <label> without htmlFor announces nothing when its control gets focus. The
# convention is htmlFor + id; this ratchet pins each file's current count of
# unassociated labels so it can only shrink. (Shared chrome — Layout.tsx —
# is already at zero and stays there by the default-zero rule.)
UNASSOCIATED_LABEL_ALLOWANCE = {
    "views/Dashboards.tsx": 15,
    "views/Datasets.tsx": 11,
    "views/Health.tsx": 1,
    "views/IcebergManager.tsx": 3,
    "views/Ontology.tsx": 1,
    "views/Schedules.tsx": 9,
    "views/Sources.tsx": 16,
    "views/Workbench.tsx": 2,
    "views/Workspaces.tsx": 3,
    "views/admin/AlertsSection.tsx": 6,
    "views/admin/DataSecuritySection.tsx": 1,
    "views/admin/EnginesSection.tsx": 5,
    "views/admin/GroupsSection.tsx": 2,
    "views/admin/MarkingsSection.tsx": 5,
    "views/admin/PortabilitySection.tsx": 6,
    "views/flow/StepForm.tsx": 18,
    "views/ontology/ActionForm.tsx": 1,
    "views/ontology/EditLogPanel.tsx": 3,
    "views/ontology/WritebackPanel.tsx": 3,
    "views/workspaces/ManageMembers.tsx": 2,
    "views/workspaces/RenameModal.tsx": 2,
}


def test_unassociated_labels_never_multiply():
    for path in view_sources() + [SRC / "Layout.tsx", SRC / "ui.tsx"]:
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        labels = len(re.findall(r"<label\b", text))
        associated = text.count("htmlFor")
        offenders = labels - associated
        allowed = UNASSOCIATED_LABEL_ALLOWANCE.get(rel, 0)
        assert offenders <= allowed, (
            f"{rel} has {offenders} <label> element(s) without htmlFor; its "
            f"ratchet allows {allowed}. Associate labels with htmlFor + id "
            "(and lower the ratchet when you fix existing ones)."
        )


# A file input hidden with display:none is out of the tab order entirely, so
# the upload flow around it is mouse-only (and any :focus-within styling on
# its label can never fire). The fix is a visually-hidden-but-focusable input.
# Ratcheted at the current offender; its owner lowers the entry when fixed.
HIDDEN_FILE_INPUT_ALLOWANCE = {
    # Datasets.tsx's import dropzone was fixed in the DATA pass -> 0.
    "views/admin/PortabilitySection.tsx": 1,
}


def test_no_file_input_is_hidden_from_the_keyboard():
    pattern = re.compile(r'<input\b[^>]*type="file"[^>]*>', re.DOTALL)
    for path in view_sources():
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        offenders = sum(
            1
            for m in pattern.finditer(text)
            if 'display: "none"' in m.group(0) or "display:none" in m.group(0)
        )
        allowed = HIDDEN_FILE_INPUT_ALLOWANCE.get(rel, 0)
        assert offenders <= allowed, (
            f"{rel} hides {offenders} file input(s) with display:none; its "
            f"ratchet allows {allowed}. Use a visually-hidden-but-focusable "
            "input so Tab + Enter can open the picker."
        )


# ---------------------------------------------- fixes from the audit pass

def test_the_builds_page_never_mentions_the_retired_explore_screen():
    # The lock notice on Builds pointed authors at "Explore" after that
    # screen merged into Analyses — a door that no longer exists. "Explore"
    # cannot go into RETIRED_PHRASES (it is an ordinary verb elsewhere), so
    # the one file that regressed is pinned instead.
    src = (VIEWS / "Pipeline.tsx").read_text(encoding="utf-8")
    assert "Explore" not in src, (
        "Pipeline.tsx names Explore — that screen is now the quick chart "
        "on Analyses; say that instead"
    )


def test_the_name_rule_sentence_has_one_source():
    """A dozen surfaces gate a name on the same two regexes and were phrasing
    the rule many ways at once — a silent disable, the raw regex, literal
    markdown backticks, and several prose variants. The sentence lives in
    ui.tsx (NAME_RULE / NAME_RULE_NO_HYPHEN); every view imports it.

    Two screens gate on rules that genuinely differ (workspace slugs are
    2–48 chars; group names admit dots) and may keep their own sentence —
    a new entry here needs a rule the shared constants cannot state.
    """
    own_rule = {"Workspaces.tsx", "GroupsSection.tsx"}
    for path in view_sources():
        if path.name in own_rule:
            continue
        text = strip_comments(path.read_text(encoding="utf-8")).lower()
        assert "lowercase letters" not in text, (
            f"{path.name} spells out the name rule; import NAME_RULE / "
            "NAME_RULE_NO_HYPHEN from ui.tsx instead"
        )
        # And never the raw regex as user-facing copy.
        assert "must match ^" not in text, f"{path.name} shows a regex to a person"
    ui = (SRC / "ui.tsx").read_text(encoding="utf-8")
    assert "NAME_RULE" in ui and "NAME_RULE_NO_HYPHEN" in ui


def test_truncation_honesty_speaks_with_one_voice():
    """Four renderings of "this result is cut off" shipped at once —
    "truncated at 1000", "(first of more)", "Showing the first 200 rows…",
    "first 1,000 of a larger result". The fact has one phrasing now
    (ui.tsx's truncationNote); callers may only add an action to it."""
    for path in view_sources():
        text = strip_comments(path.read_text(encoding="utf-8")).lower()
        assert "truncated at" not in text, f"{path.name}: use truncationNote()"
        assert "first of more" not in text, f"{path.name}: use truncationNote()"
        assert "of a larger result" not in text, (
            f"{path.name} hand-rolls the truncation phrase; use truncationNote()"
        )
    assert "of a larger result" in (SRC / "ui.tsx").read_text(encoding="utf-8")


# A mutation's ErrorBox renders beside the form that retries by resubmitting,
# so it legitimately carries no onRetry. An AUTO-RUN surface's ErrorBox with
# no onRetry is a dead end (the app never retries: retry:false,
# refetchOnWindowFocus:false app-wide). This ratchet may only go DOWN; going
# up means a new dead end shipped. The list-query boxes on Schedules, Health,
# Workspaces, Sources, Admin and Audit all carry onRetry as of this baseline.
RETRYLESS_ERRORBOX_ALLOWANCE = {
    "views/Flows.tsx": 8,
    "views/Datasets.tsx": 7,
    "views/Analyses.tsx": 7,
    "views/Dashboards.tsx": 6,
    "views/Transforms.tsx": 4,
    "views/Pipeline.tsx": 4,
    "views/IcebergManager.tsx": 4,
    "views/admin/MarkingsSection.tsx": 4,
    "views/analyses/QuickChart.tsx": 3,
    "views/Workbench.tsx": 2,
    "views/Schedules.tsx": 2,
    "views/Sources.tsx": 1,
    "views/ontology/WritebackPanel.tsx": 1,
    "views/Ontology.tsx": 1,
    "views/ontology/EditLogPanel.tsx": 1,
    "views/ontology/ActionForm.tsx": 1,
    "views/Admin.tsx": 1,
    "views/admin/PortabilitySection.tsx": 1,
    "views/admin/OntologyAccessSection.tsx": 1,
    "views/admin/GroupsSection.tsx": 1,
    "views/admin/FileSecuritySection.tsx": 1,
    "views/admin/EnginesSection.tsx": 1,
    "views/admin/DatasetAccessSection.tsx": 1,
    "views/admin/DataSecuritySection.tsx": 1,
    "views/admin/ApprovalsSection.tsx": 1,
    "views/admin/AlertsSection.tsx": 1,
}


def test_error_boxes_without_retry_never_multiply():
    for path in view_sources():
        rel = str(path.relative_to(SRC)).replace("\\", "/")
        text = path.read_text(encoding="utf-8")
        count = len(re.findall(r"<ErrorBox(?![^/>]*onRetry)[^>]", text))
        allowed = RETRYLESS_ERRORBOX_ALLOWANCE.get(f"views/{rel}".replace("views/views/", "views/"), 0)
        rel_key = f"views/{rel}" if not rel.startswith("views/") else rel
        allowed = RETRYLESS_ERRORBOX_ALLOWANCE.get(rel_key, 0)
        assert count <= allowed, (
            f"{rel_key} has {count} ErrorBox(es) without onRetry; the ratchet "
            f"allows {allowed}. If this is an auto-run surface, pass onRetry "
            "(the app never retries on its own); if it is a mutation box, "
            "raise the allowance DELIBERATELY with a reason."
        )


def test_both_pipeline_delete_confirms_promise_the_datasets_survive():
    """Two tabs of one screen, one operation, opposite messages: the Visual
    tab's delete reassured "the dataset it built is kept" while the Python
    tab's threatened "This cannot be undone" (equally true of both — the
    files go, the built datasets stay). Both confirms state the same fact."""
    flows = (VIEWS / "Flows.tsx").read_text(encoding="utf-8")
    transforms = (VIEWS / "Transforms.tsx").read_text(encoding="utf-8")
    for name, src in (("Flows.tsx", flows), ("Transforms.tsx", transforms)):
        confirms = re.findall(r"window\.confirm\(`Delete[^`]*`", src)
        assert confirms, f"{name}: the delete confirm moved — update this test"
        for c in confirms:
            assert "kept" in c, (
                f"{name}: a pipeline delete confirm must state that the "
                f"built datasets are kept: {c[:90]}"
            )
        assert "cannot be undone" not in src.lower()


def test_the_flow_builders_save_click_always_answers():
    """Save with an unfinished step was a silent no-op: no error, no toast,
    button still enabled, the only explanation already on screen lower down.
    The click now either saves or renders a "Not saved" receipt next to the
    button, computed from the live issue list."""
    src = (VIEWS / "Flows.tsx").read_text(encoding="utf-8")
    assert "saveRefused" in src
    assert "Not saved" in src
    # The guard runs in the Save click itself, before the mutation.
    click = src[src.index("setSaveRefused(true)") - 400 : src.index("setSaveRefused(true)") + 200]
    assert "previewable" in click and "save.mutate" in click
