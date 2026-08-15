"""Scan authored content for things that look like credentials.

``pipelines/*.py`` is a **code-execution channel**, not a data channel:
``transforms/api.py`` ``exec``s every ``.py`` in the directory, unsandboxed, on
every build and every transform listing. An import verb that presents itself as
data movement is therefore granting code execution, and an export that carries
these files is carrying whatever an operator hard-coded into them.

Two rules follow, and they pull in opposite directions on purpose:

* **High recall, low precision.** A false positive costs one flag; a false
  negative ships a password. So the patterns match the *word*, not a value.
* **Never strip.** A transform silently edited behind the operator's back is
  worse than a refusal — the build would still run and would quietly compute
  something else. The export refuses instead, and the refusal names the flag
  that overrides it.

**Why this covers more than pipelines.** The same argument applies to every
member the archive carries byte-faithful or near-faithful, because the reason
it is not stripped is always the same: stripping would destroy the meaning.
Measured, when this module only walked ``pipelines/``:

* ``ontology/*.yml`` travelled unscanned; a DSN in an operator's extra key
  reached the archive with ``pipeline_warnings: []`` beside it.
* ``dashboards.panels_json`` carries free SQL (``DashboardPanel.sql`` is
  documented as "Arbitrary SQL over datasets"), and a ``postgres_scan()`` call
  with a password in it travelled verbatim.
* ``object_apps.config_json`` and ``schedules.targets_json`` are open-vocabulary
  authored config and did the same.

**Why the word list grew.** Measured against the list this replaced: an
``Authorization: Bearer`` header, ``passwd=``, ``jdbc:sqlserver://``,
``mongodb+srv://`` and ``rediss://`` all passed a scan that reported zero
warnings, and the export shipped them. ``_URL_USERINFO`` is the generalisation
that stops the next scheme nobody listed — any ``scheme://user:pass@`` at all.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from laurelin.export.manifest import PipelineWarning
from laurelin.transforms.flow_files import PIPELINE_FILE_SUFFIXES

_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("password", re.compile(r"password|passwd|\bpwd\b", re.I)),
    ("secret", re.compile(r"secret", re.I)),
    ("token", re.compile(r"token", re.I)),
    ("api_key", re.compile(r"api[_-]?key", re.I)),
    ("authorization", re.compile(r"authorization|\bbearer\s+\S", re.I)),
    ("credential", re.compile(r"credential", re.I)),
    ("private_key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    # Every DSN scheme that appears in this tree's own drivers, plus the two
    # prefixes (jdbc:, odbc:) that wrap another one.
    ("dsn", re.compile(
        r"\b(postgres(ql)?|mysql|mariadb|mssql|sqlserver|oracle|mongodb(\+srv)?"
        r"|rediss?|amqps?|kafka|clickhouse|snowflake|databricks|trino|presto"
        r"|grpc\+tls?|ldaps?|ftps?|smb)://|\bjdbc:|\bodbc:",
        re.I,
    )),
    # The generalisation: any URL carrying userinfo, whatever its scheme. This
    # is what catches the scheme nobody thought to list.
    ("url_userinfo", re.compile(r"://[^/\s'\"<>]+:[^/@\s'\"<>]+@")),
    ("aws_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
)

def _preview(line: str) -> str:
    """The line up to the first match, and not one character further.

    The preview exists so the operator can find the line, not so they can read
    the value out of a report — and this report is embedded in ``manifest.json``,
    which is the *first member of the archive*. Measured, when the preview
    redacted quoted values instead: a schedule target of
    ``s3://k:SCHEDPASS10@bucket/t`` was flagged correctly and then reproduced
    verbatim in the manifest, so the warning about the leak was the leak.

    Cutting at ``match.start()`` rather than masking is the only rule that
    cannot be outwitted by the shape of the value: whatever tripped the scan,
    and everything after it, is simply not here. The ``pattern`` field already
    says what matched.
    """
    trimmed = line.strip()
    cut = len(trimmed)
    for _, pattern in _PATTERNS:
        found = pattern.search(trimmed)
        if found:
            cut = min(cut, found.start())
    return trimmed[:cut][:120] + "*****"


def looks_like_a_credential(text: str) -> bool:
    """Whether any pattern matches. The predicate behind every caller here."""
    return any(pattern.search(text) for _, pattern in _PATTERNS)


def scan_text(name: str, text: str) -> list[PipelineWarning]:
    out: list[PipelineWarning] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in _PATTERNS:
            if pattern.search(line):
                out.append(PipelineWarning(
                    file=name, line=number, pattern=label, preview=_preview(line)
                ))
                break  # one warning per line: the operator reads the line, not the count
    return out


def _scan_dir(directory: Path, suffixes: tuple[str, ...], prefix: str) -> list[PipelineWarning]:
    if not directory.is_dir():
        return []
    out: list[PipelineWarning] = []
    for path in sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in suffixes
    ):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.extend(scan_text(f"{prefix}{path.name}", text))
    return out


def scan_pipelines(pipelines_dir: Path) -> list[PipelineWarning]:
    """Every credential-shaped line in every pipeline file, in file order."""
    return _scan_dir(Path(pipelines_dir), PIPELINE_FILE_SUFFIXES, "pipelines/")


def scan_ontology(ontology_dir: Path) -> list[PipelineWarning]:
    """The same scan over ontology YAML.

    No first-party ontology key holds a credential, so this is about the extra
    keys an operator leaves in a file the loader tolerates — and about the fact
    that these files travel byte-faithful, exactly like pipelines do.
    """
    return _scan_dir(Path(ontology_dir), (".yml", ".yaml"), "ontology/")


def json_values(raw: object) -> list[str]:
    """Every string leaf of a JSON column, keys excluded.

    Keys are excluded because the secret posture *keeps* them on purpose — a
    nulled value leaves its key behind so the operator can see the shape they
    have to re-fill. Scanning the key names too made the export flag its own
    redaction: an audit row of ``{"n": 0, "password": null}`` tripped the word
    ``password`` and the whole row was withheld, taking ``n`` with it.
    """
    import json

    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, TypeError):
        return [str(raw)]

    out: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for sub in node.values():
                walk(sub)
        elif isinstance(node, (list, tuple)):
            for sub in node:
                walk(sub)
        elif isinstance(node, str):
            out.append(node)

    walk(parsed)
    return out


def scan_column(table: str, row_key: str, column: str, value: object) -> list[PipelineWarning]:
    """The same scan over one authored free-form column, as it will travel.

    ``file`` is the member the value lands in and ``line`` is 0: there is no
    line number in a JSON column, and inventing one would send the operator
    looking in the wrong place. The row key is in the file field instead,
    because that is what they need in order to open the right dashboard.
    """
    if value in (None, ""):
        return []
    name = f"tables/{table}.jsonl:{row_key}:{column}"
    warnings: list[PipelineWarning] = []
    for text in json_values(value):
        warnings.extend(scan_text(name, text))
    return [w.model_copy(update={"line": 0}) for w in warnings]


def scan_columns(table: str, row_key: str, pairs: Iterable[tuple[str, object]]) -> list[PipelineWarning]:
    out: list[PipelineWarning] = []
    for column, value in pairs:
        out.extend(scan_column(table, row_key, column, value))
    return out
