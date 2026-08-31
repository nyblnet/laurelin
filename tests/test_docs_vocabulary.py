"""The docs may not keep a name the product has retired.

`tests/test_ui_consistency.py` has policed retired vocabulary in the UI since
the IA pass, and it worked: the views say "SQL", not "SQL workbench". The docs
were never in scope, so seven files went on describing a surface by a name that
no longer appears anywhere a reader can click — SECURITY.md, SCALE.md, ROADMAP.md,
PRODUCT-ANALYSIS.md and ARCHITECTURE.md all still said "workbench" at the time
this guard was written. Someone reading the security model to decide whether to
deploy Laurelin then goes looking for a page that does not exist.

Two deliberate differences from the UI guard:

* **The phrase list is its own.** The UI list bans words like "notebook" and
  "run a build" because a *control* must use one word per concept. Prose is
  allowed to say "run a build"; what it is not allowed to do is name a surface
  something the surface is not called.
* **`CHANGELOG.md` is exempt, and this is the whole reason the exemption is
  visible here rather than buried in a glob.** A changelog is a historical
  record of what shipped when. The 0.2.0 entry says "SQL workbench" because
  that is what shipped under that name; editing it would make the record lie
  about the past to make the present look tidy. Renames belong in the entry
  that performs them, not retroactively in the ones that preceded it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Written as (phrase, what to say instead), because a guard that only says
#: "no" makes the next author guess.
RETIRED_IN_DOCS = [
    ("sql workbench", 'the surface is called SQL — say "the SQL page" or "ad-hoc SQL"'),
    ("workbench", 'the surface is called SQL; "workbench" names nothing a reader can open'),
]

#: The one place the token legitimately survives: an environment variable whose
#: name is part of the deployment contract and cannot be renamed in a doc.
#: Stripped before matching rather than allow-listed per line, so a new mention
#: of the variable never has to touch this file.
ENV_VAR = re.compile(r"LAURELIN_FEDERATION_WORKBENCH")

#: A changelog is a historical record; see the module docstring.
EXEMPT = {"CHANGELOG.md"}


def doc_sources() -> list[Path]:
    files = sorted(ROOT.joinpath("docs").rglob("*.md"))
    files += [ROOT / "SECURITY.md", ROOT / "README.md"]
    files = [p for p in files if p.exists() and p.name not in EXEMPT]
    assert len(files) >= 8, "the docs tree moved — update this test"
    return files


def test_retired_vocabulary_is_gone_from_the_docs_too():
    offenders = []
    for path in doc_sources():
        text = ENV_VAR.sub(" ", path.read_text(encoding="utf-8")).lower()
        for phrase, advice in RETIRED_IN_DOCS:
            if phrase in text:
                line = next(
                    (i + 1 for i, raw in enumerate(text.splitlines()) if phrase in raw),
                    0,
                )
                offenders.append(
                    f"{path.relative_to(ROOT)}:{line}: {phrase!r} — {advice}"
                )
                break  # one report per file; the specific phrase wins
    assert not offenders, (
        "retired vocabulary in the docs (the UI stopped saying these; the docs "
        "are read by people deciding whether to deploy):\n" + "\n".join(offenders)
    )


def test_the_changelog_is_exempt_on_purpose_and_still_holds_the_old_name():
    """A guard whose exemption is untested is an exemption nobody can defend.

    If this ever fails because the CHANGELOG no longer says "workbench", the
    right move is to delete this test — not to widen the guard over history.
    """
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").lower()
    assert "workbench" in changelog, (
        "the CHANGELOG is exempt because it records what shipped under the old "
        "name; if that record is gone, the exemption has nothing left to protect"
    )
